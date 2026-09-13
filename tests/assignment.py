#!/usr/bin/env python3
"""Requests go to a worker loop that can answer them. (I-018)

Assignment was round-robin, moving on only when a worker's queue was full. A
handler that computes rather than awaits holds its loop until it returns, and
round-robin kept handing that loop its share of every other request: with one
such handler on four loops, a quarter of all requests waited out the whole
computation while three loops sat idle. Measured first with
`bench/imbalance.py` — a 50 ms handler put p99 of a trivial route at 47.7 ms.

Assignment now prefers the least-loaded worker. A held loop cannot drain, so
its load stays above the idle loops' and requests go around it.

Free-threaded only. The GIL build runs a single loop by design, so there is no
assignment to get wrong.
"""
import sys
import threading
import time

import httpx
from aether import App, Request
from aether._workers import gil_enabled
from aether.testing import TestClient

WORKERS = 4
HOLD = 0.4
#: Generous against the 0.4 s hold. A request that landed behind a held loop
#: waits a large fraction of it; one that did not answers in well under 10 ms.
CEILING = 0.15

failures: list[str] = []

app = App(openapi_url=None, docs_url=None, mcp_url=None)


@app.get("/fast")
async def fast(_: Request):
    return {"ok": True}


@app.get("/hold")
async def hold(_: Request):
    deadline = time.perf_counter() + HOLD
    spun = 0
    while time.perf_counter() < deadline:
        spun += 1
    return {"spun": spun}


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def fast_requests_avoid_held_loops(base: str, held: int) -> None:
    holders = []
    for _ in range(held):
        thread = threading.Thread(
            target=lambda: httpx.get(f"{base}/hold", timeout=10), daemon=True
        )
        thread.start()
        holders.append(thread)
    # Long enough for each hold to be claimed and running, short of finishing.
    time.sleep(0.08)

    slow = []
    with httpx.Client(base_url=base, timeout=10) as client:
        deadline = time.perf_counter() + HOLD * 0.6
        sent = 0
        while time.perf_counter() < deadline:
            started = time.perf_counter()
            response = client.get("/fast")
            took = time.perf_counter() - started
            sent += 1
            check(response.status_code == 200, f"/fast returned {response.status_code}")
            if took > CEILING:
                slow.append(round(took * 1000))

    for thread in holders:
        thread.join(timeout=5)

    check(sent >= 10, f"only {sent} fast requests fit inside the hold; the test proves nothing")
    check(
        not slow,
        f"with {held} of {WORKERS} loops held, {len(slow)} of {sent} fast requests "
        f"waited behind one: {slow} ms",
    )


def main() -> None:
    if gil_enabled():
        print("GIL build: one worker loop, nothing to assign")
        print("\nRESULT: PASS")
        sys.exit(0)

    with TestClient(app, workers=WORKERS, timeout=20) as client:
        base = str(client.http.base_url).rstrip("/")
        for held in (1, WORKERS - 1):
            step = f"fast_requests_avoid_{held}_held_loop{'s' if held > 1 else ''}"
            try:
                fast_requests_avoid_held_loops(base, held)
                print(f"  {step}: ok")
            except Exception as exc:
                failures.append(f"{step} raised {type(exc).__name__}: {exc}")
                print(f"  {step}: ERROR")
            time.sleep(HOLD)

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
