"""Oxbrook app with a fast route and two kinds of slow one, for bench/imbalance.py.

`/cpu` holds its worker loop for `OXBROOK_SLOW_MS` of pure computation: nothing
else on that loop runs until it returns. `/io` waits the same time in
`asyncio.sleep`, which frees the loop. The difference between them is the whole
question — a request assigned to a loop blocked by `/cpu` waits, one assigned to
a loop merely awaiting `/io` does not.
"""
import argparse
import asyncio
import os
import time

from oxbrook import App, Request

app = App(openapi_url=None, docs_url=None, mcp_url=None)
SLOW = int(os.environ.get("OXBROOK_SLOW_MS", "50")) / 1000


@app.get("/fast")
async def fast(_: Request):
    return {"ok": True}


@app.get("/cpu")
async def cpu(_: Request):
    # Busy-wait rather than a loop of fixed length, so the hold time is the same
    # on every machine and interpreter build.
    deadline = time.perf_counter() + SLOW
    total = 0
    while time.perf_counter() < deadline:
        total += 1
    return {"spun": total}


@app.get("/io")
async def io(_: Request):
    await asyncio.sleep(SLOW)
    return {"slept": SLOW}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=None)
    a = p.parse_args()
    app.run(port=a.port, workers=a.workers)
