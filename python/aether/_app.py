import json
import sys
from collections.abc import Callable
from typing import Any

from . import _openapi
from ._response import Response
from ._middleware import Reply, make_gate, wrap as wrap_middleware
from ._routing import RouteInfo, build_route, route_shape
from ._streams import DROP_OLDEST, Topic
from ._workers import default_workers, gil_enabled

# Requests a single worker loop will accept at once, queued plus in-flight,
# before the server sheds load. Enough to absorb a burst of fast requests
# without letting a slow handler build a backlog that every client outlives.
DEFAULT_MAX_CONCURRENCY = 1024

#: Sockets held open at once. Separate from `max_concurrency`, which bounds
#: requests handed to a worker: an idle keep-alive connection costs a file
#: descriptor without ever reaching one.
DEFAULT_MAX_CONNECTIONS = 2048

#: Seconds to wait for a handler's first response before answering 504. Zero
#: disables it, for a service whose handlers are legitimately long-running.
DEFAULT_REQUEST_TIMEOUT = 30.0
#: Seconds to let in-flight requests finish after Ctrl-C before stopping.
DEFAULT_SHUTDOWN_GRACE = 10.0

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
        access_log: bool = False,
        redis_url: str | None = None,
    ) -> None:
        """`openapi_url` and `docs_url` can each be set to None to disable them.

        `debug` returns handler exception text in the 500 response. Leave it off
        outside development: exception messages routinely carry connection
        strings, file paths and user data.

        `access_log` adds one log line per request. Opt-in: it costs something
        per request, and many deployments already log at the proxy.

        `redis_url` enables durable topics. Nothing connects until the first
        durable topic is used.

        `mcp_url` is where agents reach the service over the Model Context
        Protocol. It exposes only routes marked `tool=True`, plus topics as
        readable resources. Set it to None to turn the endpoint off entirely.
        """
        self.routes: list[RouteInfo] = []
        self._middleware: list[Any] = []
        if access_log:
            from ._logging import access_middleware

            # First registered, so it wraps everything and sees the final status.
            self._middleware.append(access_middleware)
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
            self._add(build_route(fn, method, path, tool=tool))
            return fn

        return decorator

    def _add(self, route: RouteInfo) -> None:
        """Register a route, refusing one the router could not hold.

        The radix tree rejects two routes of the same shape, and it is built
        when the server starts — on whatever thread called `serve`, which for
        the test client is a background thread where the error becomes a
        connection refused and nothing else. Checked here instead, so a
        duplicate fails at import, pointing at the decorator that caused it.

        A WebSocket route is registered as a GET, so it collides with a GET on
        the same path, which is the correct answer: only one of them could
        ever run.
        """
        shape = route_shape(route.path)
        for existing in self.routes:
            if existing.method == route.method and route_shape(existing.path) == shape:
                same = existing.path == route.path
                raise ValueError(
                    f"{route.method} {route.path} conflicts with "
                    f"{existing.method} {existing.path}: "
                    + (
                        "the same route is registered twice"
                        if same
                        else "the paths differ only in parameter names, which the "
                        "router cannot tell apart"
                    )
                )
        self.routes.append(route)

    def get(self, path: str, tool: bool = False):
        return self.route("GET", path, tool=tool)

    def post(self, path: str, tool: bool = False):
        return self.route("POST", path, tool=tool)

    def put(self, path: str, tool: bool = False):
        return self.route("PUT", path, tool=tool)

    def delete(self, path: str, tool: bool = False):
        return self.route("DELETE", path, tool=tool)

    def middleware(self, fn):
        """Register middleware, which runs around every HTTP handler.

            @app.middleware
            async def require_key(request, call_next):
                if request.header("x-api-key") != SECRET:
                    return Reply({"error": "unauthorized"}, status=401)
                return await call_next(request)

        Runs outermost-first in registration order. WebSocket routes are not
        wrapped: their handshake completes before the handler runs, so there is
        nothing useful to intercept yet.
        """
        self._middleware.append(fn)
        return fn

    def websocket(self, path: str, authorize: Any = None):
        """Register a WebSocket endpoint.

        `authorize` runs before the handshake and can refuse the upgrade,
        which the handler cannot: by the time it runs, the 101 has been sent
        and the client believes it is connected. Return None or True to
        accept, or a `Response`/`Reply` to refuse.

            async def members_only(request):
                if not valid(request.header("authorization")):
                    return Response(b"nope", status=401, content_type="text/plain")

            @app.websocket("/ws", authorize=members_only)
            async def feed(request, ws): ...
        

        The handler takes the request and the socket. Aether performs the
        handshake, so the socket is already open when the handler runs, and the
        connection closes when it returns.

            @app.websocket("/ws")
            async def echo(request, ws):
                async for message in ws:
                    await ws.send(message)
        """

        def decorator(fn):
            route = build_route(fn, "GET", path, websocket=True)
            if authorize is not None:
                route.authorizer = make_gate(authorize)
            self._add(route)
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
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
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

        `request_timeout` is how long to wait for a handler's first response
        before answering 504. It does not cut short a stream that has already
        started, so SSE and WebSocket are unaffected. Zero disables it.

        `shutdown_grace` is how long Ctrl-C waits for in-flight requests to
        finish before stopping anyway.

        `max_connections` caps sockets held open. At the limit the server stops
        accepting rather than refusing, so the wait lands in the OS backlog.
        """
        server = self.build_server(
            host, port, workers, max_concurrency, max_body, request_timeout,
            shutdown_grace, max_connections, announce=True,
        )
        try:
            server.serve()
        except KeyboardInterrupt:
            pass

    def build_server(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        workers: int | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_body: int = DEFAULT_MAX_BODY,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        announce: bool = False,
    ):
        """Prepare a server without starting it.

        `run` uses this; so does the test client, which needs to start the
        server on one thread and stop it from another.
        """
        from ._core import Server

        workers = workers or default_workers()
        mode = "GIL" if gil_enabled() else "free-threaded"
        if announce:
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
            (
                r.method,
                r.path,
                # Sockets are left alone, and a route pays nothing when no
                # middleware is registered.
                r.target
                if r.websocket or not self._middleware
                else wrap_middleware(r.target, self._middleware),
                [p.as_spec() for p in r.params],
                r.websocket,
                # Middleware wraps the authorizer too, so an app-wide auth rule
                # covers sockets even though it cannot wrap the handler itself.
                None
                if r.authorizer is None
                else (
                    r.authorizer
                    if not self._middleware
                    else wrap_middleware(r.authorizer, self._middleware)
                ),
            )
            for r in self.routes
        ]
        return Server(
            host,
            port,
            workers,
            max_concurrency,
            max_body,
            self.debug,
            request_timeout,
            shutdown_grace,
            max_connections,
            not announce,
            specs,
        )
