# Requests

The first argument to every handler is the request.

```python
@app.get("/me")
async def me(request: Request):
    token = request.header("authorization")        # case-insensitive, None if absent
    theme = request.cookies.get("theme", "light")
    return {"token": token, "theme": theme}
```

| attribute | what it is |
|---|---|
| `method` | the HTTP method, uppercase |
| `path` | the request path |
| `query` | the raw query string |
| `body` | the raw body as `bytes` |
| `headers` | every header, lowercased, as a dict |
| `cookies` | parsed cookies as a dict |
| `header(name, default=None)` | one header by name, case-insensitively |

## Headers are lazy

Headers stay in hyper's own map until Python asks for them. `request.header(name)`
looks one up without building anything; `request.headers` builds the whole dict
and should be avoided on a hot path. A handler that never reads a header pays
nothing for the ones that arrived.

A header that is not valid UTF-8 reads as absent rather than raising, which
keeps a malformed request from becoming a `500`.

## The request is immutable

`Request` is a frozen class. Nothing on the Python side can mutate it, which is
why it needs no locking even when several worker loops are running in the same
process on a free-threaded build. Pass values between middleware and handlers
through a `ContextVar` or a dependency, not by attaching attributes to the
request.

## Repeated headers

Repeated headers are joined with `", "`, as HTTP itself defines. `Cookie` is
parsed for you into `request.cookies`.
