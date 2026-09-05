"""Aether basics: routes, typed path parameters, and raw request access."""

from aether import App, Request

app = App()


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


# `{*name}` captures the rest of the path, and is always a str.
@app.get("/files/{*rest}")
async def get_file(_: Request, rest: str):
    return {"path": rest}


@app.post("/echo")
async def echo(req: Request):
    return {"path": req.path, "query": req.query, "body": req.body.decode()}


if __name__ == "__main__":
    app.run(port=8000)
