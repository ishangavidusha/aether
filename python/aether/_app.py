import json
import sys
from collections.abc import Callable
from typing import Any

from . import _openapi
from ._response import Response
from ._routing import RouteInfo, build_route
from ._streams import DROP_OLDEST, Topic
from ._workers import default_workers, gil_enabled

# Requests a single worker loop will accept at once, queued plus in-flight,
# before the server sheds load. Enough to absorb a burst of fast requests
# without letting a slow handler build a backlog that every client outlives.
DEFAULT_MAX_CONCURRENCY = 1024

#: Largest request body accepted, in bytes. Without a cap a single request can
#: grow the process by several times the payload before a handler sees it.
DEFAULT_MAX_BODY = 16 * 1024 * 1024


class App:
    def __init__(
        self,
        title: str = "Aether",
        version: str = "0.1.0",
        description: str = "",
        openapi_url: str | None = "/openapi.json",
        docs_url: str | None = "/docs",
        mcp_url: str | None = "/mcp",
        debug: bool = False,
        redis_url: str | None = None,
    ) -> None:
        """`openapi_url` and `docs_url` can each be set to None to disable them.

        `debug` returns handler exception text in the 500 response. Leave it off
        outside development: exception messages routinely carry connection
        strings, file paths and user data.

        `redis_url` enables durable topics. Nothing connects until the first
        durable topic is used.

        `mcp_url` is where agents reach the service over the Model Context
        Protocol. It exposes only routes marked `tool=True`, plus topics as
        readable resources. Set it to None to turn the endpoint off entirely.
        """
        self.routes: list[RouteInfo] = []
        self._topics: dict[str, Topic] = {}
        self.title = title
        self.version = version
        self.description = description
        self.openapi_url = openapi_url
        self.docs_url = docs_url
        self.mcp_url = mcp_url
        self.debug = debug
        self.redis_url = redis_url
        self._backend: Any = None

    def route(self, method: str, path: str, tool: bool = False):
        """Register a route.

        `tool=True` also exposes it to agents over MCP. Opt-in on purpose:
        every route being agent-callable by default would mean an
        administrative delete endpoint is agent-callable by default.
        """
        method = method.upper()

        def decorator(fn):
            # Validates the handler against its path and fails here, at import
            # time, rather than on the first request.
            self.routes.append(build_route(fn, method, path, tool=tool))
            return fn

        return decorator

    def get(self, path: str, tool: bool = False):
        return self.route("GET", path, tool=tool)

    def post(self, path: str, tool: bool = False):
        return self.route("POST", path, tool=tool)

    def put(self, path: str, tool: bool = False):
        return self.route("PUT", path, tool=tool)

    def delete(self, path: str, tool: bool = False):
        return self.route("DELETE", path, tool=tool)

    def websocket(self, path: str):
        """Register a WebSocket endpoint.

        The handler takes the request and the socket. Aether performs the
        handshake, so the socket is already open when the handler runs, and the
        connection closes when it returns.

            @app.websocket("/ws")
            async def echo(request, ws):
                async for message in ws:
                    await ws.send(message)
        """

        def decorator(fn):
            self.routes.append(build_route(fn, "GET", path, websocket=True))
            return fn

        return decorator

    def backend(self) -> Any:
        """The shared Redis backend, connected lazily on first use."""
        if self._backend is None:
            if not self.redis_url:
                raise RuntimeError(
                    "durable topics need a redis_url: App(redis_url='redis://...')"
                )
            from ._redis import RedisBackend

            self._backend = RedisBackend(self.redis_url)
        return self._backend

    def topic(
        self,
        name: str,
        maxsize: int | None = None,
        policy: str | None = None,
        durable: bool = False,
    ) -> Topic:
        """Get or create a named topic.

        Shared across every worker loop in the process, so a message emitted by
        one handler reaches subscribers running on all of them. `durable=True`
        additionally shares it across processes and records it in Redis, which
        needs `App(redis_url=...)`.

        `maxsize`, `policy` and `durable` apply only when the topic is first
        created.
        """
        existing = self._topics.get(name)
        if existing is not None:
            return existing
        created = Topic(
            name,
            maxsize=maxsize or 1024,
            policy=policy or DROP_OLDEST,
            backend=self.backend() if durable else None,
        )
        # Racing handlers could both create one; keep whichever landed first so
        # every worker sees the same object.
        return self._topics.setdefault(name, created)

    @property
    def topics(self) -> dict[str, Topic]:
        return dict(self._topics)

    def capabilities(self) -> dict[str, Any]:
        """The capabilities this service exposes to agents.

        Built from the routes marked `tool=True`, without starting a server, so
        it can be inspected or checked into a test.
        """
        from . import _capabilities

        return _capabilities.build(self.routes)

    def openapi(self) -> dict[str, Any]:
        """The OpenAPI 3.1 document for the routes registered so far.

        Built from the same metadata the router uses, so it cannot describe an
        endpoint the server would not accept. Callable without running the
        server, which makes it usable for client generation in CI.
        """
        return _openapi.build(self.routes, self.title, self.version, self.description)

    def _register_mcp(self) -> None:
        """Add the agent endpoint, unless it was turned off."""
        if not self.mcp_url:
            return
        if ("POST", self.mcp_url) in {(r.method, r.path) for r in self.routes}:
            return

        from ._mcp import MCP

        # Capabilities are built once, at start, so a malformed one is an error
        # at boot rather than on an agent's first call.
        server = MCP(self, self.capabilities())

        @self.post(self.mcp_url)
        async def mcp_endpoint(request):
            """Model Context Protocol endpoint."""
            return await server.handle(request.body)

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
        max_body: int = DEFAULT_MAX_BODY,
    ) -> None:
        """Serve until interrupted.

        `max_concurrency` bounds the requests a single worker loop will accept
        at once, counting both those queued and those already running. When
        every worker is at its limit the server answers 503 rather than growing
        without bound. Lower it for slow handlers, where a deep backlog only
        adds latency before an inevitable client timeout; raise it to absorb
        larger bursts of fast requests.

        `max_body` caps a request body; anything larger is answered 413 without
        being buffered.
        """
        from ._core import Server

        workers = workers or default_workers()
        mode = "GIL" if gil_enabled() else "free-threaded"
        print(
            f"Aether: {workers} worker loop(s), max {max_concurrency} concurrent/worker, "
            f"{mode} Python {sys.version_info.major}.{sys.version_info.minor}"
            f"{', debug' if self.debug else ''}",
            flush=True,
        )
        # Docs first: the document is built from the routes registered so far,
        # so registering /mcp afterwards keeps it out of the OpenAPI paths.
        self._register_docs()
        self._register_mcp()
        specs = [
            (r.method, r.path, r.target, [p.as_spec() for p in r.params], r.websocket)
            for r in self.routes
        ]
        try:
            Server(
                host, port, workers, max_concurrency, max_body, self.debug, specs
            ).serve()
        except KeyboardInterrupt:
            pass
