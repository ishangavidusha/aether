import json
import sys
from collections.abc import Callable
from typing import Any

from . import _openapi
from ._response import Response
from ._routing import RouteInfo, build_route
from ._workers import default_workers, gil_enabled

# Requests a single worker loop will accept at once, queued plus in-flight,
# before the server sheds load. Enough to absorb a burst of fast requests
# without letting a slow handler build a backlog that every client outlives.
DEFAULT_MAX_CONCURRENCY = 1024


class App:
    def __init__(
        self,
        title: str = "Aether",
        version: str = "0.1.0",
        description: str = "",
        openapi_url: str | None = "/openapi.json",
        docs_url: str | None = "/docs",
    ) -> None:
        """`openapi_url` and `docs_url` can each be set to None to disable them."""
        self.routes: list[RouteInfo] = []
        self.title = title
        self.version = version
        self.description = description
        self.openapi_url = openapi_url
        self.docs_url = docs_url

    def route(self, method: str, path: str):
        method = method.upper()

        def decorator(fn):
            # Validates the handler against its path and fails here, at import
            # time, rather than on the first request.
            self.routes.append(build_route(fn, method, path))
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

    def openapi(self) -> dict[str, Any]:
        """The OpenAPI 3.1 document for the routes registered so far.

        Built from the same metadata the router uses, so it cannot describe an
        endpoint the server would not accept. Callable without running the
        server, which makes it usable for client generation in CI.
        """
        return _openapi.build(self.routes, self.title, self.version, self.description)

    def _register_docs(self) -> None:
        """Add the OpenAPI and docs routes, unless the user turned them off."""
        registered = {(r.method, r.path) for r in self.routes}

        if self.openapi_url and ("GET", self.openapi_url) not in registered:
            # Serialized once at startup, not per request.
            document = json.dumps(self.openapi()).encode()

            @self.get(self.openapi_url)
            async def openapi_json(_request):
                """OpenAPI schema."""
                return Response(document, content_type="application/json")

        if self.docs_url and self.openapi_url and ("GET", self.docs_url) not in registered:
            page = _openapi.DOCS_TEMPLATE.format(
                title=self.title, openapi_url=self.openapi_url
            ).encode()

            @self.get(self.docs_url)
            async def docs(_request):
                """API documentation."""
                return Response(page, content_type="text/html; charset=utf-8")

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
        self._register_docs()
        specs = [
            (r.method, r.path, r.target, [p.as_spec() for p in r.params])
            for r in self.routes
        ]
        try:
            Server(host, port, workers, max_concurrency, specs).serve()
        except KeyboardInterrupt:
            pass
