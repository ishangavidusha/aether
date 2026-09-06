#!/usr/bin/env python3
"""Regression tests for three defects found on 2026-09-06.

Each of these was demonstrated against a running server before being fixed:

* `HEAD` returned 405 on every route, which violates HTTP.
* A crashing handler returned its exception text to the client. The probe used
  a fake database URL with a password in it and the client received it.
* A request body was buffered without limit. One 200MB POST took the server
  from 44MB to 836MB resident.
"""
import sys
import threading

import httpx

from aether import App, Request

PORT = 8807
DEBUG_PORT = 8808
BASE = f"http://127.0.0.1:{PORT}"
LIMIT = 64 * 1024

SECRET = "db://user:hunter2@internal-host/prod"

app = App(openapi_url=None, docs_url=None)


@app.get("/hello")
async def hello(_: Request):
    return {"hello": "world"}


@app.post("/only-post")
async def only_post(_: Request):
    return {"ok": True}


@app.post("/echo")
async def echo(req: Request):
    return {"len": len(req.body)}


@app.get("/boom")
async def boom(_: Request):
    raise ValueError(f"connection failed for {SECRET}")


@app.websocket("/ws")
async def ws(_: Request, socket):
    async for message in socket:
        await socket.send(message)


debug_app = App(openapi_url=None, docs_url=None, debug=True)


@debug_app.get("/boom")
async def debug_boom(_: Request):
    raise ValueError(f"connection failed for {SECRET}")


def check(failures, cond, msg):
    if not cond:
        failures.append(msg)


def main() -> None:
    failures: list[str] = []
    threading.Thread(
        target=lambda: app.run(port=PORT, max_body=LIMIT), daemon=True
    ).start()
    threading.Thread(target=lambda: debug_app.run(port=DEBUG_PORT), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/hello", timeout=0.3)
            httpx.get(f"http://127.0.0.1:{DEBUG_PORT}/boom", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    with httpx.Client(base_url=BASE, timeout=30) as c:
        # --- HEAD ---
        body = c.get("/hello").content
        r = c.head("/hello")
        check(failures, r.status_code == 200, f"HEAD on a GET route returned {r.status_code}")
        check(failures, r.content == b"", "HEAD returned a body")
        check(
            failures,
            r.headers.get("content-length") == str(len(body)),
            f"HEAD content-length was {r.headers.get('content-length')!r}, "
            f"expected {len(body)}",
        )
        check(
            failures,
            r.headers.get("content-type") == "application/json",
            f"HEAD content-type was {r.headers.get('content-type')!r}",
        )
        check(
            failures,
            c.head("/only-post").status_code == 405,
            "HEAD on a POST-only route should be 405",
        )
        check(failures, c.head("/nope").status_code == 404, "HEAD on an unknown path")
        check(
            failures,
            c.head("/ws").status_code in (405, 426),
            f"HEAD on a socket route returned {c.head('/ws').status_code}",
        )

        # --- error responses must not leak ---
        r = c.get("/boom")
        check(failures, r.status_code == 500, f"crashing handler returned {r.status_code}")
        check(failures, SECRET not in r.text, "500 response leaked the exception text")
        check(
            failures,
            "ValueError" not in r.text,
            "500 response leaked the exception type",
        )

    with httpx.Client(base_url=f"http://127.0.0.1:{DEBUG_PORT}", timeout=10) as c:
        r = c.get("/boom")
        check(
            failures,
            SECRET in r.text,
            "debug=True should include the exception detail, for development",
        )

    with httpx.Client(base_url=BASE, timeout=60) as c:
        # --- body limit ---
        ok = c.post("/echo", content=b"x" * (LIMIT - 1024))
        check(failures, ok.status_code == 200, f"body under the limit returned {ok.status_code}")
        big = c.post("/echo", content=b"x" * (LIMIT * 4))
        check(failures, big.status_code == 413, f"oversized body returned {big.status_code}")
        # The server must still be healthy afterwards.
        check(failures, c.get("/hello").status_code == 200, "server unhealthy after a 413")

    print(f"checks: {'PASS' if not failures else 'FAIL'}")
    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
