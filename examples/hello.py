"""Aether basics: routes, typed parameters, validated bodies, and OpenAPI.

Run it, then open http://127.0.0.1:8000/docs
"""

from pydantic import BaseModel, Field

from aether import App, Request, Response

app = App(title="Aether Example", version="0.1.0")


@app.get("/")
async def hello(_: Request):
    return {"hello": "world"}


# Path parameters are declared in the path and typed by the handler's
# annotations. Coercion happens in Rust, so `/users/abc` is rejected with a 422
# without ever reaching Python.
@app.get("/users/{user_id}")
async def get_user(_: Request, user_id: int):
    return {"user_id": user_id, "type": type(user_id).__name__}


@app.get("/orgs/{org}/repos/{repo}/issues/{number}")
async def get_issue(_: Request, org: str, repo: str, number: int):
    return {"org": org, "repo": repo, "number": number}


# Arguments that are not in the path are query parameters. A default makes one
# optional, and `str | None` makes it optional and nullable.
@app.get("/search")
async def search(_: Request, q: str, limit: int = 10, cursor: str | None = None):
    """Search everything.

    `q` is required, so requesting /search without it returns a 422.
    """
    return {"q": q, "limit": limit, "cursor": cursor}


# `{*name}` captures the rest of the path, and is always a str.
@app.get("/files/{*rest}")
async def get_file(_: Request, rest: str):
    return {"path": rest}


class UserIn(BaseModel):
    name: str = Field(min_length=1)
    age: int = Field(ge=0)


class UserOut(BaseModel):
    id: int
    name: str


# An argument annotated with a pydantic model binds the request body. Returning
# a model serializes it, and only the fields that model declares are sent.
@app.post("/users")
async def create_user(_: Request, body: UserIn):
    return UserOut(id=1, name=body.name)


@app.post("/echo")
async def echo(req: Request):
    return {"path": req.path, "query": req.query, "body": req.body.decode()}


# Return a Response when you need a specific status code or content type.
@app.get("/teapot")
async def teapot(_: Request):
    return Response(b"short and stout", status=418, content_type="text/plain")


if __name__ == "__main__":
    app.run(port=8000)
