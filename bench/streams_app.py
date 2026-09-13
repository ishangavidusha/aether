"""Aether app for bench/streams.py: one topic fanned out over SSE, and an echo socket.

The topic blocks rather than drops, so a publish only finishes when every
subscriber has taken every event. That makes elapsed time a lossless delivery
rate instead of a count of how much was thrown away.
"""
import argparse
import time

from aether import SSE, App, Request

app = App(openapi_url=None, docs_url=None, mcp_url=None)
ticks = app.topic("ticks", maxsize=1024, policy="block")


@app.get("/sse")
async def sse(_: Request):
    return SSE(ticks.subscribe(), ping=None)


@app.get("/cpu")
async def cpu(_: Request):
    """This process's CPU seconds. Read from inside, because `ps` rounds to
    whole seconds on Linux, which is most of a short run."""
    return {"cpu": time.process_time()}


@app.get("/subscribers")
async def subscribers(_: Request):
    return {"count": ticks.subscribers}


@app.post("/publish")
async def publish(_: Request, n: int, size: int = 64):
    payload = "x" * size
    started = time.perf_counter()
    for i in range(n):
        await ticks.emit({"i": i, "p": payload})
    return {"elapsed": time.perf_counter() - started, "subscribers": ticks.subscribers}


@app.websocket("/echo")
async def echo(_request, ws):
    async for message in ws:
        await ws.send(message)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=None)
    a = p.parse_args()
    # Publishing a large batch is one long request; streams hold connections.
    app.run(port=a.port, workers=a.workers, request_timeout=0, max_connections=16384)
