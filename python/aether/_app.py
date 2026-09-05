import sys
from collections.abc import Callable
from typing import Any

from ._routing import bind_body, build_spec
from ._workers import default_workers, gil_enabled

# (method, path, handler, [(param name, param type)])
Route = tuple[str, str, Callable[..., Any], list[tuple[str, str]]]

# Requests a single worker loop will accept at once, queued plus in-flight,
# before the server sheds load. Enough to absorb a burst of fast requests
# without letting a slow handler build a backlog that every client outlives.
DEFAULT_MAX_CONCURRENCY = 1024


class App:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def route(self, method: str, path: str):
        method = method.upper()

        def decorator(fn):
            # Validates the handler against its path and fails here, at import
            # time, rather than on the first request.
            spec, body = build_spec(fn, method, path)
            target = fn if body is None else bind_body(fn, *body)
            self._routes.append((method, path, target, spec))
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

    def run(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        workers: int | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        """Serve until interrupted.

        `max_concurrency` bounds the requests a single worker loop will accept
        at once, counting both those queued and those already running. When
        every worker is at its limit the server answers 503 rather than growing
        without bound. Lower it for slow handlers, where a deep backlog only
        adds latency before an inevitable client timeout; raise it to absorb
        larger bursts of fast requests.
        """
        from ._core import Server

        workers = workers or default_workers()
        mode = "GIL" if gil_enabled() else "free-threaded"
        print(
            f"Aether: {workers} worker loop(s), max {max_concurrency} concurrent/worker, "
            f"{mode} Python {sys.version_info.major}.{sys.version_info.minor}",
            flush=True,
        )
        try:
            Server(host, port, workers, max_concurrency, self._routes).serve()
        except KeyboardInterrupt:
            pass
