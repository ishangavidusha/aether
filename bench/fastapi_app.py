"""FastAPI equivalent of examples/hello.py."""
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


@app.get("/")
async def hello():
    return {"hello": "world"}


@app.get("/users/{user_id}")
async def user(user_id: int):
    return {"user_id": user_id}


class UserIn(BaseModel):
    name: str
    age: int
    email: str


class UserOut(BaseModel):
    id: int
    name: str
    age: int


@app.post("/users", response_model=UserOut)
async def create_user(body: UserIn):
    return UserOut(id=1, name=body.name, age=body.age)


@app.get("/search")
async def search(q: str, limit: int = 10):
    return {"q": q, "limit": limit}
