#!/usr/bin/env python3
"""Correctness checks for queue-based dispatch.

The dispatch path hands a oneshot channel across threads, so the failure mode
that matters is a response being delivered to the wrong request. This drives
concurrent traffic with a unique token per request and asserts every response
carries its own token back.

    tests/verify.py                     # uses the running interpreter
"""
import asyncio
import sys
import threading

import httpx

from aether import App, Request

PORT = 8791
calls = 0
lock = threading.Lock()
threads_seen: set[str] = set()

app = App()


@app.post("/echo")
async def echo(req: Request):
    global calls
    with lock:
        calls += 1
        threads_seen.add(threading.current_thread().name)
    return {"token": req.body.decode()}


@app.get("/stats")
async def stats(_: Request):
    with lock:
        return {"calls": calls, "threads": sorted(threads_seen)}


async def drive(n: int, concurrency: int) -> tuple[int, int]:
    limits = httpx.Limits(max_connections=concurrency)
    ok = bad = 0
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", limits=limits) as c:
        sem = asyncio.Semaphore(concurrency)

        async def one(i: int):
            nonlocal ok, bad
            token = f"tok-{i}-{'x' * (i % 97)}"
            async with sem:
                r = await c.post("/echo", content=token)
            if r.status_code == 200 and r.json()["token"] == token:
                ok += 1
            else:
                bad += 1

        await asyncio.gather(*(one(i) for i in range(n)))
    return ok, bad


def main() -> None:
    n, concurrency = 5000, 64
    server = threading.Thread(target=lambda: app.run(port=PORT), daemon=True)
    server.start()

    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/stats", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    ok, bad = asyncio.run(drive(n, concurrency))
    final = httpx.get(f"http://127.0.0.1:{PORT}/stats", timeout=5).json()

    gil = "GIL" if sys._is_gil_enabled() else "free-threaded"
    print(f"build            : {gil} Python {sys.version_info.major}.{sys.version_info.minor}")
    print(f"requests sent    : {n} at {concurrency} concurrent")
    print(f"token matched    : {ok}")
    print(f"token mismatched : {bad}")
    print(f"handler calls    : {final['calls']} (expected {n})")
    print(f"worker threads   : {len(final['threads'])} -> {final['threads']}")

    failures = []
    if bad:
        failures.append(f"{bad} responses did not match their request")
    if ok != n:
        failures.append(f"only {ok}/{n} succeeded")
    if final["calls"] != n:
        failures.append(f"handler ran {final['calls']} times, expected {n}")

    print("\nRESULT:", "FAIL - " + "; ".join(failures) if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
