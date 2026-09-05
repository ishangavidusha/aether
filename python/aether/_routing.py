"""Turning a handler signature into a route spec.

Path parameters are declared in the path as `{name}`, or `{*name}` to capture
the rest of the path. Their types come from the handler's annotations, and the
Rust router coerces them before a worker is ever woken.

A handler argument annotated with a pydantic model binds the request body.

Everything here runs once, at registration. Mistakes surface at import time with
a message naming the handler, rather than as a confusing 500 on the first
request.
"""

import inspect
import re
import typing
from collections.abc import Callable
from typing import Any

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
# src/router.rs.
_SUPPORTED: dict[Any, str] = {str: "str", int: "int", float: "float", bool: "bool"}


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


def build_spec(
    fn: Callable[..., Any], method: str, path: str
) -> tuple[list[tuple[str, str]], tuple[str, Any] | None]:
    """Validate the handler against its path.

    Returns the path parameter spec and, if the handler declares one, the name
    and model of its body argument.
    """
    where = f"{method} {path} -> {fn.__qualname__}"

    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"{where}: handlers must be `async def`")

    if "{" in _PLACEHOLDER.sub("", path) or "}" in _PLACEHOLDER.sub("", path):
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

    accepted = positional[1:]
    accepted_names = {p.name for p in accepted}
    hints = _annotations(fn)

    missing = [n for n in names if n not in accepted_names]
    if missing:
        raise TypeError(
            f"{where}: path declares {', '.join(repr(n) for n in missing)} "
            f"but the handler does not accept "
            f"{'them' if len(missing) > 1 else 'it'}"
        )

    # Anything not in the path must be a pydantic model, which becomes the body.
    body: tuple[str, Any] | None = None
    leftover = [p for p in accepted if p.name not in set(names)]
    for param in leftover:
        annotation = hints.get(param.name)
        if is_model(annotation):
            if body is not None:
                raise TypeError(
                    f"{where}: handler declares two body models, "
                    f"{body[0]!r} and {param.name!r}. Only one is allowed"
                )
            body = (param.name, annotation)
            continue
        hint = (
            "install pydantic and annotate it with a BaseModel to bind the request body"
            if not HAVE_PYDANTIC
            else "annotate it with a pydantic BaseModel to bind the request body, "
            "or read query parameters from `request.query`"
        )
        raise TypeError(
            f"{where}: handler accepts {param.name!r}, which is not a path "
            f"parameter. Query parameter binding is not implemented yet; {hint}"
        )

    spec: list[tuple[str, str]] = []
    for name, is_wildcard in declared:
        annotation = hints.get(name, str)
        if is_wildcard and annotation is not str:
            raise TypeError(
                f"{where}: wildcard parameter {name!r} captures the rest of the "
                f"path and must be annotated `str`"
            )
        kind = _SUPPORTED.get(annotation)
        if kind is None:
            supported = ", ".join(t.__name__ for t in _SUPPORTED)
            raise TypeError(
                f"{where}: path parameter {name!r} is annotated "
                f"{getattr(annotation, '__name__', annotation)!r}, which is not "
                f"supported. Use one of: {supported}"
            )
        spec.append((name, kind))
    return spec, body
