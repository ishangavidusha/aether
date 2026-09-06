"""Turning a handler signature into a route.

Path parameters are declared in the path as `{name}`, or `{*name}` to capture
the rest of the path. Any other argument is a query parameter, unless it is
annotated with a pydantic model, in which case it binds the request body.

Types come from the handler's annotations. Path and query parameters are coerced
by the Rust router before a worker is ever woken; bodies are validated by
pydantic on the worker thread.

Everything here runs once, at registration. Mistakes surface at import time with
a message naming the handler, rather than as a confusing 500 later.
"""

import datetime as _datetime
import inspect
import re
import types
import typing
import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ._depends import Depends, bind as bind_dependencies, declared
from ._schema import (
    HAVE_PYDANTIC,
    RequestValidationError,
    ValidationError,
    is_model,
    validation_body,
)

# Matches {name} and {*name}. Deliberately strict: an unbalanced or oddly named
# placeholder should be a clear error, not a route that silently never matches.
_PLACEHOLDER = re.compile(r"\{(\*?)([A-Za-z_][A-Za-z0-9_]*)\}")

# Types the Rust side knows how to coerce. Keep in sync with ParamKind in
# src/router.rs. The richer three are validated in Rust and built into Python
# objects on the worker thread.
_SUPPORTED: dict[Any, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    _uuid.UUID: "uuid",
    # datetime is a subclass of date, so order matters only to a reader; the
    # lookup is by exact type.
    _datetime.datetime: "datetime",
    _datetime.date: "date",
}

_EMPTY = inspect.Parameter.empty


@dataclass(slots=True)
class ParamInfo:
    name: str
    kind: str
    source: str  # "path" or "query"
    presence: str  # "required", "omit" or "null"
    annotation: Any = str
    default: Any = None
    optional: bool = False
    repeated: bool = False
    """`list[T]`: collect every occurrence rather than the first."""

    def as_spec(self) -> tuple[str, str, str, str, bool]:
        """The tuple the Rust router expects."""
        return (self.name, self.kind, self.source, self.presence, self.repeated)


@dataclass(slots=True)
class RouteInfo:
    method: str
    path: str
    fn: Callable[..., Any]
    """The function the user wrote, kept for documentation."""
    target: Callable[..., Any]
    """What actually runs, which wraps `fn` when there is a body to validate."""
    params: list[ParamInfo] = field(default_factory=list)
    body: tuple[str, Any] | None = None
    dependencies: dict[str, Any] = field(default_factory=dict)
    response_model: Any = None
    summary: str = ""
    description: str = ""
    websocket: bool = False
    authorizer: Any = None
    """Runs before the handshake; may refuse the upgrade."""
    tool: bool = False
    """Exposed to agents over MCP. Opt-in, never the default."""


def path_params(path: str) -> list[tuple[str, bool]]:
    """(name, is_wildcard) for each placeholder, in path order."""
    return [(name, star == "*") for star, name in _PLACEHOLDER.findall(path)]


def _annotations(fn: Callable[..., Any]) -> dict[str, Any]:
    try:
        return typing.get_type_hints(fn)
    except Exception:
        # A forward reference we cannot resolve should not break registration;
        # unannotated parameters fall back to str.
        return dict(getattr(fn, "__annotations__", {}))


def _unwrap_list(annotation: Any) -> tuple[Any, bool]:
    """`list[int]` -> (int, True). A bare `list` is rejected: without an item
    type there is nothing to coerce to."""
    if typing.get_origin(annotation) is list:
        args = typing.get_args(annotation)
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """`int | None` -> (int, True). Anything else -> (annotation, False)."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def bind_body(fn: Callable[..., Any], name: str, model: Any) -> Callable[..., Any]:
    """Wrap a handler so its body argument is validated before it runs.

    Only routes that declare a body pay for this extra frame.
    """

    async def handler(request, **params):
        try:
            params[name] = model.model_validate_json(request.body)
        except ValidationError as exc:
            raise RequestValidationError(validation_body(exc)) from None
        return await fn(request, **params)

    handler.__name__ = getattr(fn, "__name__", "handler")
    handler.__qualname__ = getattr(fn, "__qualname__", "handler")
    return handler


def _build_target(fn: Callable[..., Any], body: Any, dependencies: dict) -> Callable[..., Any]:
    """Layer body validation and dependency resolution around the handler.

    Dependencies resolve outside body validation, so a dependency that opens a
    resource still tears it down when the body turns out to be invalid.
    """
    target = fn if body is None else bind_body(fn, *body)
    return target if not dependencies else bind_dependencies(target, dependencies)


def build_route(
    fn: Callable[..., Any],
    method: str,
    path: str,
    websocket: bool = False,
    tool: bool = False,
) -> RouteInfo:
    where = f"{'WEBSOCKET' if websocket else method} {path} -> {getattr(fn, '__qualname__', fn)}"

    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"{where}: handlers must be `async def`")

    stripped = _PLACEHOLDER.sub("", path)
    if "{" in stripped or "}" in stripped:
        raise ValueError(
            f"{where}: malformed path parameter. Use {{name}} or {{*name}}, "
            f"with a name like a Python identifier"
        )

    declared = path_params(path)
    names = [name for name, _ in declared]
    if len(set(names)) != len(names):
        raise ValueError(f"{where}: duplicate path parameter name")

    positional = [
        p
        for p in inspect.signature(fn).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    if not positional:
        raise TypeError(f"{where}: handler must accept the request as its first argument")

    if websocket:
        if len(positional) < 2:
            raise TypeError(
                f"{where}: a websocket handler takes the request and the socket, "
                f"as `async def {getattr(fn, '__name__', 'handler')}(request, ws, ...)`"
            )
        accepted = {p.name: p for p in positional[2:]}
    else:
        accepted = {p.name: p for p in positional[1:]}
    hints = _annotations(fn)

    missing = [n for n in names if n not in accepted]
    if missing:
        raise TypeError(
            f"{where}: path declares {', '.join(repr(n) for n in missing)} "
            f"but the handler does not accept "
            f"{'them' if len(missing) > 1 else 'it'}"
        )

    params: list[ParamInfo] = []
    for name, is_wildcard in declared:
        annotation = hints.get(name, str)
        if is_wildcard and annotation is not str:
            raise TypeError(
                f"{where}: wildcard parameter {name!r} captures the rest of the "
                f"path and must be annotated `str`"
            )
        kind = _SUPPORTED.get(annotation)
        if kind is None:
            raise TypeError(
                f"{where}: path parameter {name!r} is annotated "
                f"{getattr(annotation, '__name__', annotation)!r}, which is not "
                f"supported. Use one of: {', '.join(t.__name__ for t in _SUPPORTED)}"
            )
        params.append(
            ParamInfo(name, kind, "path", "required", annotation=annotation)
        )

    # Anything left is a query parameter, the body if it is a pydantic model,
    # or a dependency if its default says so.
    body: tuple[str, Any] | None = None
    dependencies: dict[str, Depends] = {}
    for name, param in accepted.items():
        if name in set(names):
            continue

        if isinstance(param.default, Depends):
            dependencies[name] = param.default
            continue
        annotation = hints.get(name, _EMPTY)

        if is_model(annotation):
            if websocket:
                raise TypeError(
                    f"{where}: a websocket handler has no request body; "
                    f"read messages from the socket instead"
                )
            if body is not None:
                raise TypeError(
                    f"{where}: handler declares two body models, "
                    f"{body[0]!r} and {name!r}. Only one is allowed"
                )
            body = (name, annotation)
            continue

        if annotation is _EMPTY:
            raise TypeError(
                f"{where}: query parameter {name!r} needs a type annotation. "
                f"Use one of: {', '.join(t.__name__ for t in _SUPPORTED)}"
            )

        base, optional = _unwrap_optional(annotation)
        base, repeated = _unwrap_list(base)
        kind = _SUPPORTED.get(base)
        if kind is None:
            extra = (
                ", or a pydantic BaseModel to bind the request body"
                if HAVE_PYDANTIC
                else ""
            )
            raise TypeError(
                f"{where}: {name!r} is annotated "
                f"{getattr(base, '__name__', base)!r}, which is not supported as a "
                f"query parameter. Use one of: "
                f"{', '.join(t.__name__ for t in _SUPPORTED)}{extra}"
            )

        if param.default is not _EMPTY:
            # Leave it out of the kwargs and let Python apply the default.
            presence = "omit"
        elif optional:
            presence = "null"
        else:
            presence = "required"

        params.append(
            ParamInfo(
                name,
                kind,
                "query",
                presence,
                annotation=base,
                default=None if param.default is _EMPTY else param.default,
                optional=optional,
                repeated=repeated,
            )
        )

    response_model = hints.get("return")
    if not is_model(response_model):
        response_model = None

    doc = inspect.getdoc(fn) or ""
    summary, _, description = doc.partition("\n\n")

    return RouteInfo(
        method=method,
        path=path,
        fn=fn,
        target=_build_target(fn, body, dependencies),
        params=params,
        body=body,
        dependencies=dependencies,
        response_model=response_model,
        summary=summary.strip().replace("\n", " "),
        description=description.strip(),
        websocket=websocket,
        tool=tool,
    )
