"""Aether hello-world with CLI args, used by bench/run.py."""
import argparse

from aether import App, Request

app = App()


@app.get("/")
async def hello(_: Request):
    return {"hello": "world"}


@app.get("/users/{user_id}")
async def user(_: Request, user_id: int):
    return {"user_id": user_id}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=None)
    a = p.parse_args()
    app.run(port=a.port, workers=a.workers)
