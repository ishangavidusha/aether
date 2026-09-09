#!/usr/bin/env python3
"""Dependency injection, sessions, and logging."""
import io
import json
import logging
import sys

from aether import App, Depends, Request, Sessions
from aether._logging import JsonFormatter
from aether.testing import TestClient

failures: list[str] = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def dependencies() -> None:
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    events: list[str] = []

    async def config():
        events.append("config")
        return {"dsn": "db://x"}

    async def db(cfg=Depends(config)):
        events.append("open")
        try:
            yield f"conn:{cfg['dsn']}"
        finally:
            events.append("close")

    def caller(request):
        return request.header("x-user", "anon")

    @app.get("/q")
    async def q(_: Request, conn=Depends(db), user=Depends(caller), cfg=Depends(config)):
        events.append("handler")
        return {"conn": conn, "user": user, "dsn": cfg["dsn"]}

    @app.get("/boom")
    async def boom(_: Request, conn=Depends(db)):
        raise ValueError("handler failed")

    with TestClient(app) as c:
        body = c.get("/q", headers={"x-user": "ada"}).json()
        check(body["conn"] == "conn:db://x", f"dependency value was {body['conn']!r}")
        check(body["user"] == "ada", "the request was not passed to a dependency")
        check(
            events == ["config", "open", "handler", "close"],
            f"resolution order was {events}",
        )
        check(events.count("config") == 1, "a shared dependency ran more than once")

        events.clear()
        check(c.get("/boom").status_code == 500, "a raising handler should be a 500")
        check(
            "close" in events,
            "teardown did not run when the handler raised, so a resource leaks",
        )


def no_cache() -> None:
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    calls = []

    def token():
        calls.append(1)
        return len(calls)

    @app.get("/twice")
    async def twice(
        _: Request, a=Depends(token, use_cache=False), b=Depends(token, use_cache=False)
    ):
        return {"a": a, "b": b}

    with TestClient(app) as c:
        body = c.get("/twice").json()
    check(body["a"] != body["b"], f"use_cache=False still cached: {body}")


def sessions_round_trip() -> None:
    store = Sessions(secret="unit-test-secret", secure=False)
    app = App(openapi_url=None, docs_url=None, mcp_url=None)
    app.middleware(store.middleware)

    @app.get("/bump")
    async def bump(_: Request, session=Depends(store.load)):
        session["n"] = session.get("n", 0) + 1
        return {"n": session["n"]}

    @app.get("/peek")
    async def peek(_: Request, session=Depends(store.load)):
        return {"n": session.get("n", 0)}

    with TestClient(app) as c:
        first = c.get("/bump")
        check(first.json() == {"n": 1}, f"first bump gave {first.json()}")
        check("set-cookie" in first.headers, "no session cookie was set")
        check("HttpOnly" in first.headers["set-cookie"], "session cookie is not HttpOnly")
        check(c.get("/bump").json() == {"n": 2}, "session did not persist")

        unchanged = c.get("/peek")
        check(unchanged.json() == {"n": 2}, "reading the session lost its contents")
        check(
            "set-cookie" not in unchanged.headers,
            "an unmodified session still rewrote its cookie",
        )

        # A forged or edited cookie must be treated as no session at all.
        c.http.cookies.set("aether_session", "ZmFrZQ.bm90LWEtc2lnbmF0dXJl", domain="127.0.0.1")
        check(c.get("/peek").json() == {"n": 0}, "a forged session cookie was trusted")


def session_signing() -> None:
    store = Sessions(secret="a", secure=False)
    other = Sessions(secret="b", secure=False)
    token = store.encode({"user": "ada"})
    check(store.decode(token) == {"user": "ada"}, "a session did not round-trip")
    check(other.decode(token) == {}, "a session signed with another key was accepted")
    payload, _, sig = token.partition(".")
    check(store.decode(f"{payload}x.{sig}") == {}, "an edited payload was accepted")
    check(store.decode("garbage") == {}, "a malformed cookie raised instead of failing shut")
    expired = Sessions(secret="a", max_age=-1, secure=False)
    check(expired.decode(expired.encode({"x": 1})) == {}, "an expired session was accepted")


def logging_output() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("aether")
    previous, previous_level = root.handlers[:], root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    root.propagate = False

    try:
        app = App(openapi_url=None, docs_url=None, mcp_url=None, access_log=True)

        @app.get("/ok")
        async def ok(_: Request):
            return {"ok": True}

        @app.get("/boom")
        async def boom(_: Request):
            raise ValueError("leaky detail")

        with TestClient(app) as c:
            c.get("/ok")
            check(c.get("/boom").status_code == 500, "boom should be a 500")
    finally:
        root.handlers[:] = previous
        root.setLevel(previous_level)

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    check(bool(lines), "nothing was logged")
    access = [line for line in lines if line["logger"] == "aether.access"]
    check(len(access) == 2, f"expected 2 access lines, got {len(access)}")
    check(
        access[0]["status"] == 200 and access[0]["path"] == "/ok",
        f"access line was {access[0]}",
    )
    check("duration_ms" in access[0], "access line has no duration")
    check(access[1]["status"] == 500, f"failed request logged as {access[1]['status']}")

    tracebacks = [line for line in lines if "exception" in line]
    check(len(tracebacks) == 1, f"traceback logged {len(tracebacks)} times, expected once")
    check(
        "leaky detail" in tracebacks[0]["exception"],
        "the traceback did not include the error",
    )


def main() -> None:
    for step in (dependencies, no_cache, sessions_round_trip, session_signing, logging_output):
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
