"""FastAPI equivalent of examples/hello.py."""
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
async def hello():
    return {"hello": "world"}


@app.get("/users/{user_id}")
async def user(user_id: int):
    return {"user_id": user_id}
