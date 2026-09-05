"""Aether app with a deliberately CPU-bound handler.

Hello-world benchmarks measure dispatch. This one measures whether Python
handler *execution* runs in parallel across worker loops, which is the whole
argument for targeting free-threaded CPython.
"""
import argparse

from aether import App, Request

app = App()
ITERATIONS = 20_000


@app.get("/cpu")
async def cpu(_: Request):
    total = 0
    for i in range(ITERATIONS):
        total += i * i
    return {"total": total}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=None)
    a = p.parse_args()
    app.run(port=a.port, workers=a.workers)
