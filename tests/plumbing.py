#!/usr/bin/env python3
"""Milestone 6: headers, middleware, timeouts, graceful shutdown, test client.

The unglamorous half. None of it is conceptually interesting and all of it
decides whether the framework is usable.
"""
import asyncio
import sys
import threading
import time

from aether import App, Reply, Request, Response
from aether.testing import TestClient

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


# --------------------------------------------------------------------------
def headers_and_cookies() -> None:
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/inspect")
    async def inspect(req: Request):
        return {
            "auth": req.header("authorization"),
            "auth_upper": req.header("AUTHORIZATION"),
            "absent": req.header("x-nope"),
            "absent_default": req.header("x-nope", "fallback"),
            "cookies": dict(req.cookies),
            "has_accept": "accept" in req.headers,
        }

    with TestClient(app) as c:
        body = c.get(
            "/inspect",
            headers={
                "Authorization": "Bearer tok",
                "Cookie": "session=abc; theme=dark",
                "Accept": "application/json",
            },
        ).json()
    check(body["auth"] == "Bearer tok", f"header read gave {body['auth']!r}")
    check(body["auth_upper"] == "Bearer tok", "header lookup is not case-insensitive")
    check(body["absent"] is None, f"missing header gave {body['absent']!r}")
    check(body["absent_default"] == "fallback", "header default ignored")
    check(
        body["cookies"] == {"session": "abc", "theme": "dark"},
        f"cookies parsed as {body['cookies']}",
    )
    check(body["has_accept"], "headers dict is missing a header that was sent")


# --------------------------------------------------------------------------
def middleware() -> None:
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    order: list[str] = []

    @app.middleware
    async def outer(request, call_next):
        order.append("outer-in")
        reply = await call_next(request)
        order.append("outer-out")
        reply.headers["x-outer"] = "1"
        return reply

    @app.middleware
    async def inner(request, call_next):
        order.append("inner-in")
        reply = await call_next(request)
        order.append("inner-out")
        return reply

    @app.middleware
    async def guard(request, call_next):
        if request.header("x-api-key") != "let-me-in":
            return Reply({"error": "unauthorized"}, status=401)
        return await call_next(request)

    @app.get("/open")
    async def open_route(_: Request):
        order.append("handler")
        return {"ok": True}

    @app.get("/as-response")
    async def as_response(_: Request):
        return Response(b'{"custom":true}', status=201, headers={"x-handler": "yes"})

    with TestClient(app) as c:
        blocked = c.get("/open")
        check(blocked.status_code == 401, f"guard let a request through: {blocked.status_code}")
        check(blocked.json() == {"error": "unauthorized"}, "short-circuit body wrong")
        check("handler" not in order, "handler ran despite the guard refusing")

        order.clear()
        allowed = c.get("/open", headers={"x-api-key": "let-me-in"})
        check(allowed.status_code == 200, f"allowed request returned {allowed.status_code}")
        check(
            order == ["outer-in", "inner-in", "handler", "inner-out", "outer-out"],
            f"middleware order was {order}",
        )
        check(
            allowed.headers.get("x-outer") == "1",
            "middleware could not add a response header",
        )

        wrapped = c.get("/as-response", headers={"x-api-key": "let-me-in"})
        check(wrapped.status_code == 201, f"Response status lost: {wrapped.status_code}")
        check(
            wrapped.headers.get("x-handler") == "yes"
            and wrapped.headers.get("x-outer") == "1",
            "headers from handler and middleware were not merged",
        )


def no_middleware_is_untouched() -> None:
    """A route pays nothing when nothing is registered."""
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/plain")
    async def plain(_: Request):
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/plain")
    check(r.status_code == 200 and r.json() == {"ok": True}, "plain route broke")


# --------------------------------------------------------------------------
def request_timeout() -> None:
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/slow")
    async def slow(_: Request):
        await asyncio.sleep(5)
        return {"never": True}

    @app.get("/quick")
    async def quick(_: Request):
        return {"ok": True}

    with TestClient(app, request_timeout=0.5, timeout=20) as c:
        started = time.perf_counter()
        r = c.get("/slow")
        elapsed = time.perf_counter() - started
        check(r.status_code == 504, f"a stalled handler returned {r.status_code}")
        check(elapsed < 3, f"504 took {elapsed:.1f}s, so the timeout did not fire")
        # The server must still be healthy afterwards.
        check(c.get("/quick").status_code == 200, "server unhealthy after a timeout")


def timeout_does_not_cut_streams() -> None:
    """SSE holds a connection open far longer than the timeout on purpose."""
    from aether import SSE

    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/feed")
    async def feed(_: Request):
        async def source():
            for i in range(3):
                yield {"n": i}
                await asyncio.sleep(0.4)

        return SSE(source(), ping=None)

    with TestClient(app, request_timeout=0.5, timeout=20) as c:
        with c.stream("GET", "/feed") as response:
            check(response.status_code == 200, f"SSE returned {response.status_code}")
            text = "".join(response.iter_text())
    check(text.count("data:") == 3, f"stream was cut short: {text.count('data:')} events")


def streams_release_their_slot() -> None:
    """A finished stream must free its concurrency slot immediately.

    It did not: the SSE path forms a reference cycle, so the slot was only
    freed when Python's cyclic collector happened to run. With a small limit
    that means a server refusing traffic it has capacity for, and a shutdown
    that cannot drain. The slot is now released explicitly.
    """
    from aether import SSE

    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/sse")
    async def sse(_: Request):
        async def source():
            yield "one"

        return SSE(source(), ping=None)

    @app.get("/probe")
    async def probe(_: Request):
        return {"ok": True}

    limit = 4
    with TestClient(app, workers=1, max_concurrency=limit, timeout=20) as c:
        for i in range(limit * 3):
            with c.stream("GET", "/sse") as response:
                body = "".join(response.iter_text())
            check("data: one" in body, f"stream {i} was empty")
        # Far more streams than the limit have completed. If any leaked a slot,
        # the worker would now be refusing requests.
        after = c.get("/probe")
        check(
            after.status_code == 200,
            f"after {limit * 3} finished streams the server returned "
            f"{after.status_code}, so slots leaked",
        )

        started = time.perf_counter()
    drain = time.perf_counter() - started
    check(drain < 3.0, f"shutdown took {drain:.1f}s, so a finished stream held a slot")


# --------------------------------------------------------------------------
def abandoned_stream_does_not_wedge_shutdown() -> None:
    """A client that walks away from an idle stream, then an immediate stop.

    This is the sequence that deadlocked the GIL build (I-038). The disconnect
    was reported by attaching to the interpreter from a tokio thread and calling
    `loop.call_soon_threadsafe`, which blocks writing to the loop's self-pipe.
    On the GIL build that thread holds the lock while blocked, so the thread
    trying to shut the server down never runs again and the process stops dead.
    Notifications now travel through the worker's wake queue instead.

    The deadlock needed Linux and a loaded machine to show itself; CI reproduced
    it every run and this laptop never did. What this check guarantees is that
    the path is exercised on both builds, and that shutdown after an abandoned
    stream still returns promptly.
    """
    from aether import SSE

    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/idle")
    async def idle(_: Request):
        async def source():
            yield "first"
            # Nothing else, ever: the stream is parked in __anext__ exactly as
            # a subscription to a quiet topic would be.
            await asyncio.Event().wait()

        return SSE(source(), ping=None)

    client = TestClient(app, workers=1, timeout=20, shutdown_grace=5.0).start()
    try:
        with client.stream("GET", "/idle") as response:
            check(response.status_code == 200, f"stream returned {response.status_code}")
            # Read only the first event, then abandon the connection.
            for _ in response.iter_lines():
                break
        started = time.perf_counter()
    finally:
        client.stop()
    drain = time.perf_counter() - started
    check(drain < 5.0, f"shutdown took {drain:.1f}s after an abandoned stream")


# --------------------------------------------------------------------------
def graceful_shutdown() -> None:
    """In-flight work finishes instead of being dropped."""
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    finished = threading.Event()

    @app.get("/work")
    async def work(_: Request):
        await asyncio.sleep(1.0)
        finished.set()
        return {"done": True}

    @app.get("/ready")
    async def ready(_: Request):
        return {"ok": True}

    client = TestClient(app, timeout=20, shutdown_grace=10.0).start()
    outcome: dict = {}

    def slow_call():
        try:
            outcome["status"] = client.get("/work").status_code
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    caller = threading.Thread(target=slow_call)
    caller.start()
    time.sleep(0.3)  # let the request reach the handler

    stopped = time.perf_counter()
    client.stop()
    drain = time.perf_counter() - stopped
    caller.join(timeout=20)

    check(finished.is_set(), "in-flight handler was dropped instead of finishing")
    check(
        outcome.get("status") == 200,
        f"in-flight client got {outcome.get('status') or outcome.get('error')}",
    )
    check(drain >= 0.4, f"shutdown returned in {drain:.2f}s, so it did not wait")


def shutdown_grace_expires() -> None:
    """A handler that outlasts the grace period is abandoned, not waited for.

    The branch that gives up had never run: every other test drains in time, so
    the deadline was only ever a number. Rust coverage found the line.
    """
    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/forever")
    async def forever(_: Request):
        await asyncio.sleep(30)
        return {"never": True}

    client = TestClient(app, timeout=20, shutdown_grace=0.5).start()

    def call():
        try:
            client.get("/forever")
        except Exception:
            pass

    caller = threading.Thread(target=call, daemon=True)
    caller.start()
    time.sleep(0.3)

    stopped = time.perf_counter()
    client.stop()
    drain = time.perf_counter() - stopped

    check(drain >= 0.4, f"shutdown returned in {drain:.2f}s without waiting out the grace")
    check(
        drain < 5.0,
        f"shutdown took {drain:.1f}s, so it waited for a handler it should have abandoned",
    )


def connection_cap() -> None:
    """More sockets than the cap must queue, not fail.

    `max_concurrency` bounds requests handed to a worker; an idle keep-alive
    connection never reaches one, so it needs its own limit.
    """
    import concurrent.futures

    import httpx

    app = App(openapi_url=None, docs_url=None, mcp_url=None)

    @app.get("/ok")
    async def ok(_: Request):
        await asyncio.sleep(0.05)
        return {"ok": True}

    with TestClient(app, max_connections=2, timeout=30) as c:
        def one(_):
            # A fresh connection each time, so the cap is what is under test
            # rather than a shared pool.
            with httpx.Client(base_url=c.base_url, timeout=30) as client:
                return client.get("/ok").status_code

        with concurrent.futures.ThreadPoolExecutor(10) as pool:
            codes = list(pool.map(one, range(10)))

    check(
        codes == [200] * 10,
        f"a cap of 2 should queue the rest, not fail them: {sorted(codes)}",
    )


# --------------------------------------------------------------------------
def test_client_transports() -> None:
    from aether import SSE

    app = App(title="T", version="1.0")

    @app.get("/thing/{n}", tool=True)
    async def thing(_: Request, n: int):
        """Get a thing."""
        return {"n": n}

    @app.websocket("/ws")
    async def ws(_: Request, socket):
        async for message in socket:
            await socket.send(f"echo:{message}")

    @app.get("/sse")
    async def sse(_: Request):
        async def source():
            yield "one"

        return SSE(source(), ping=None)

    with TestClient(app) as c:
        check(c.get("/thing/7").json() == {"n": 7}, "test client GET failed")
        check(c.get("/thing/x").status_code == 422, "coercion did not run via the client")
        check(c.call_tool("thing", {"n": 9}) == {"n": 9}, "call_tool failed")
        tools = c.mcp("tools/list")["tools"]
        check([t["name"] for t in tools] == ["thing"], f"tools/list gave {tools}")

        with c.stream("GET", "/sse") as response:
            body = "".join(response.iter_text())
        check("data: one" in body, f"SSE via the client gave {body!r}")

        async def socket_round_trip():
            async with c.websocket("/ws") as socket:
                await socket.send("hi")
                return await asyncio.wait_for(socket.recv(), 5)

        check(
            asyncio.run(socket_round_trip()) == "echo:hi",
            "websocket via the client failed",
        )


def main() -> None:
    steps = [
        headers_and_cookies,
        middleware,
        no_middleware_is_untouched,
        request_timeout,
        timeout_does_not_cut_streams,
        streams_release_their_slot,
        abandoned_stream_does_not_wedge_shutdown,
        graceful_shutdown,
        shutdown_grace_expires,
        connection_cap,
        test_client_transports,
    ]
    for step in steps:
        try:
            step()
            print(f"  {step.__name__}: ok")
        except Exception as exc:
            failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  {step.__name__}: ERROR")

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
