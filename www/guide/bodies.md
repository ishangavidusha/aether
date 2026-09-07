# Request bodies

An argument annotated with a pydantic model binds the request body.

```python
from pydantic import BaseModel

class UserIn(BaseModel):
    name: str
    age: int

class UserOut(BaseModel):
    id: int
    name: str

@app.post("/users")
async def create_user(_: Request, body: UserIn) -> UserOut:
    return UserOut(id=1, name=body.name)
```

Returning a model serializes it, and only the fields that model declares are
sent — a `UserOut` returned from a handler that also holds a password hash
sends the fields of `UserOut`.

## Validation failures

A body that fails validation returns `422` carrying pydantic's own errors, in
the same `{"detail": [...]}` shape as a path or query parameter failure. A
client parses one format for every `422`, wherever it came from.

```json
{
  "detail": [
    {
      "type": "int_parsing",
      "loc": ["body", "age"],
      "msg": "Input should be a valid integer, ..."
    }
  ]
}
```

Body validation runs on the worker thread, not in Rust. It is the one place
Aether wakes Python before rejecting bad input, because pydantic is the
validator and pydantic is Python. It costs about 13% against hello world:

| target | req/s | vs Aether |
|---|---:|---:|
| Aether, hello world | 184,861 | 1.0x |
| Aether, validated POST | 158,031 | 1.2x |
| granian + FastAPI, validated POST | 17,023 | 10.9x |
| uvicorn + FastAPI, validated POST | 9,948 | 18.6x |

Both sides run the same pydantic version on the same models, so that gap is
dispatch and serialization, not validation.

## Raw bodies

`request.body` is the raw bytes, for a handler that wants to parse them itself.

```python
@app.post("/webhook")
async def webhook(request: Request):
    verify_signature(request.header("x-signature"), request.body)
    return {"ok": True}
```

## pydantic is optional

Aether imports and runs without pydantic installed. Only body models and model
responses need it. Everything else — routing, parameters, topics, sockets — is
plain Python and Rust.

## Size limits

Bodies are capped at 16 MiB by default. Anything larger is answered `413`
without being buffered, so a large upload cannot grow the process before a
handler ever sees it. Raise it per server:

```python
app.run(max_body=64 * 1024 * 1024)
```
