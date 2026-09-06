"""Dependency injection.

    async def get_db(request):
        db = await pool.acquire()
        try:
            yield db
        finally:
            await pool.release(db)

    @app.get("/users")
    async def list_users(_: Request, db = Depends(get_db)):
        return await db.fetch("select ...")

A dependency is any callable. It may take the request or take nothing, be sync
or async, and may itself declare dependencies. An async generator gets its
teardown run after the handler returns, which is the main reason to have this
rather than calling a function at the top of every handler.

Results are cached per request, so a dependency shared by three others runs
once. Cleanup runs in reverse order, like a stack of context managers.

Sync dependencies are called on the worker loop and must not block: there is no
thread offload, by the same rule that makes handlers `async def` only.
"""

import inspect
from typing import Any


class Depends:
    """Marks a handler argument as supplied by a dependency."""

    __slots__ = ("dependency", "use_cache")

    def __init__(self, dependency: Any, *, use_cache: bool = True) -> None:
        if not callable(dependency):
            raise TypeError(
                f"Depends() needs a callable, got {type(dependency).__name__}"
            )
        self.dependency = dependency
        self.use_cache = use_cache

    def __repr__(self) -> str:
        return f"Depends({getattr(self.dependency, '__name__', self.dependency)})"


def _wants_request(fn: Any) -> bool:
    """True if the dependency takes a positional argument for the request."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in signature.parameters.values():
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            if isinstance(param.default, Depends):
                continue
            return True
        break
    return False


def declared(fn: Any) -> dict[str, Depends]:
    """The `Depends(...)` arguments a callable declares."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    return {
        name: param.default
        for name, param in signature.parameters.items()
        if isinstance(param.default, Depends)
    }


async def _resolve(
    marker: Depends, request: Any, cache: dict, cleanups: list, depth: int = 0
) -> Any:
    if depth > 20:
        raise RuntimeError(
            f"dependency nesting is too deep at {marker!r}; this is almost "
            f"certainly a cycle"
        )

    fn = marker.dependency
    if marker.use_cache and fn in cache:
        return cache[fn]

    kwargs: dict[str, Any] = {}
    for name, sub in declared(fn).items():
        kwargs[name] = await _resolve(sub, request, cache, cleanups, depth + 1)

    args = (request,) if _wants_request(fn) else ()
    produced = fn(*args, **kwargs)

    if inspect.isasyncgen(produced):
        value = await produced.__anext__()
        # Closed after the handler, which is what makes `try/finally` around a
        # yield work the way it reads.
        cleanups.append(produced)
    elif inspect.isgenerator(produced):
        value = next(produced)
        cleanups.append(produced)
    elif inspect.isawaitable(produced):
        value = await produced
    else:
        value = produced

    if marker.use_cache:
        cache[fn] = value
    return value


async def _close(resource: Any) -> None:
    if inspect.isasyncgen(resource):
        await resource.aclose()
    else:
        resource.close()


def bind(handler: Any, dependencies: dict[str, Depends]) -> Any:
    """Wrap a handler so its dependencies are resolved per request."""

    async def wrapped(request, **params):
        cache: dict[Any, Any] = {}
        cleanups: list[Any] = []
        try:
            for name, marker in dependencies.items():
                params[name] = await _resolve(marker, request, cache, cleanups)
            return await handler(request, **params)
        finally:
            # Reverse order, so a dependency is torn down before anything it
            # was built from.
            for resource in reversed(cleanups):
                try:
                    await _close(resource)
                except Exception:  # noqa: BLE001 - one failure must not hide others
                    from ._logging import logger

                    logger.exception("dependency teardown failed")

    wrapped.__name__ = getattr(handler, "__name__", "handler")
    wrapped.__qualname__ = getattr(handler, "__qualname__", "handler")
    return wrapped
