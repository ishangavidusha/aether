"""Aether app whose handler burns a configurable amount of CPU.

`AETHER_SWEEP_ITERS` sets the loop length. Zero means the handler does nothing
but return, which is the hello-world case. This is the knob the worker-count
sweep turns.
"""
import argparse
import os

from aether import App, Request

app = App()
ITERS = int(os.environ.get("AETHER_SWEEP_ITERS", "0"))


@app.get("/work")
async def work(_: Request):
    total = 0
    for i in range(ITERS):
        total += i * i
    return {"total": total}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=None)
    a = p.parse_args()
    app.run(port=a.port, workers=a.workers)
