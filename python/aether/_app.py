import inspect
import sys
from collections.abc import Callable
from typing import Any

from ._workers import default_workers, gil_enabled


class App:
    def __init__(self) -> None:
        self._routes: list[tuple[str, str, Callable[..., Any]]] = []

    def route(self, method: str, path: str):
        def decorator(fn):
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"Aether handlers must be `async def` (got {fn.__qualname__})"
                )
            self._routes.append((method.upper(), path, fn))
            return fn

        return decorator

    def get(self, path: str):
        return self.route("GET", path)

    def post(self, path: str):
        return self.route("POST", path)

    def put(self, path: str):
        return self.route("PUT", path)

    def delete(self, path: str):
        return self.route("DELETE", path)

    def run(self, host: str = "127.0.0.1", port: int = 8000, workers: int | None = None) -> None:
        from ._core import Server

        workers = workers or default_workers()
        mode = "GIL" if gil_enabled() else "free-threaded"
        print(
            f"Aether: {workers} worker loop(s), {mode} Python "
            f"{sys.version_info.major}.{sys.version_info.minor}",
            flush=True,
        )
        try:
            Server(host, port, workers, self._routes).serve()
        except KeyboardInterrupt:
            pass
