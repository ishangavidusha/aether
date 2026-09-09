"""Aether hello-world with CLI args, used by bench/run.py."""
import argparse

from aether import App, Request
from pydantic import BaseModel

app = App()


@app.get("/")
async def hello(_: Request):
    return {"hello": "world"}


@app.get("/users/{user_id}")
async def user(_: Request, user_id: int):
    return {"user_id": user_id}


class UserIn(BaseModel):
    name: str
    age: int
    email: str


class UserOut(BaseModel):
    id: int
    name: str
    age: int


@app.post("/users")
async def create_user(_: Request, body: UserIn):
    return UserOut(id=1, name=body.name, age=body.age)


@app.get("/search")
async def search(_: Request, q: str, limit: int = 10):
    return {"q": q, "limit": limit}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=None)
    a = p.parse_args()
    app.run(port=a.port, workers=a.workers)
