import sys
from collections.abc import Callable
from typing import Any

from ._routing import build_spec
from ._workers import default_workers, gil_enabled

# (method, path, handler, [(param name, param type)])
Route = tuple[str, str, Callable[..., Any], list[tuple[str, str]]]


class App:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def route(self, method: str, path: str):
        method = method.upper()

        def decorator(fn):
            # Validates the handler against its path and fails here, at import
            # time, rather than on the first request.
            self._routes.append((method, path, fn, build_spec(fn, method, path)))
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
