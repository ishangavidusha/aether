#!/usr/bin/env python3
"""Backpressure: the server sheds load instead of growing without bound.

The handler here awaits rather than burning CPU, which is the case a queue
bound alone would miss. The drain callback empties the queue into asyncio tasks
immediately, so the queue stays near empty while in-flight requests pile up.
Only a limit counting both catches this.
"""
import asyncio
import sys
import threading

import httpx
from aether import App, Request

PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
LIMIT = 5
HOLD = 0.4

app = App()


@app.get("/slow")
async def slow(_: Request):
    await asyncio.sleep(HOLD)
    return {"ok": True}


@app.get("/fast")
async def fast(_: Request):
    return {"ok": True}


async def flood(n: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    async with httpx.AsyncClient(base_url=BASE, timeout=20,
                                 limits=httpx.Limits(max_connections=n)) as c:
        results = await asyncio.gather(*(c.get("/slow") for _ in range(n)),
                                       return_exceptions=True)
    for r in results:
        key = -1 if isinstance(r, BaseException) else r.status_code
        counts[key] = counts.get(key, 0) + 1
    return counts


def main() -> None:
    threading.Thread(
        target=lambda: app.run(port=PORT, workers=1, max_concurrency=LIMIT),
        daemon=True,
    ).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/fast", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    sent = 40
    counts = asyncio.run(flood(sent))
    accepted = counts.get(200, 0)
    shed = counts.get(503, 0)

    retry_after = False
    probe = httpx.get(f"{BASE}/fast", timeout=5)  # server still alive?
    recovered = probe.status_code == 200

    # One more flood to confirm the Retry-After header is present on a 503.
    with httpx.Client(base_url=BASE, timeout=20) as c:
        with httpx.Client(base_url=BASE, timeout=20) as bg:
            threads = [threading.Thread(target=lambda: bg.get("/slow")) for _ in range(LIMIT + 3)]
            for t in threads:
                t.start()
            threading.Event().wait(0.1)
            r = c.get("/slow")
            if r.status_code == 503:
                retry_after = "retry-after" in {k.lower() for k in r.headers}
            for t in threads:
                t.join()

    print(f"limit            : {LIMIT} concurrent per worker, 1 worker")
    print(f"sent             : {sent} at once, each holding {HOLD}s")
    print(f"200 accepted     : {accepted}")
    print(f"503 shed         : {shed}")
    other = {k: v for k, v in counts.items() if k not in (200, 503)}
    print(f"other/errors     : {other or 'none'}")
    print(f"alive after load : {recovered}")
    print(f"Retry-After on 503: {retry_after}")

    failures = []
    if accepted == 0:
        failures.append("nothing was accepted at all")
    if shed == 0:
        failures.append("nothing was shed, so the limit is not enforced")
    if accepted + shed != sent:
        failures.append(f"{sent - accepted - shed} requests neither succeeded nor were shed")
    # Accepted may exceed LIMIT as earlier requests finish and free capacity,
    # but it must not approach the whole flood or the limit means nothing.
    if accepted > sent // 2:
        failures.append(f"accepted {accepted} of {sent}, limit is not constraining")
    if not recovered:
        failures.append("server did not accept a request after the load stopped")
    if not retry_after:
        failures.append("503 did not carry a Retry-After header")

    print("\nRESULT:", "FAIL - " + "; ".join(failures) if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
