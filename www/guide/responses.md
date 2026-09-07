# Responses

Return whatever the handler has. Aether decides how to send it.

| returned | sent as |
|---|---|
| `dict`, `list`, `str`, `int`, and friends | JSON, serialized in Rust |
| a pydantic model | JSON, through pydantic's own serializer |
| `None` | `204 No Content` |
| [`Response`](../reference/http.md#aether.Response) | exactly what it says |
| [`SSE`](../streams/sse.md) | a `text/event-stream` that stays open |

## Explicit responses

Return a `Response` when you need a specific status code, a content type that
is not JSON, or bytes that are already encoded and should not be touched.

```python
from aether import Response

@app.get("/teapot")
async def teapot(_: Request):
    return Response(b"short and stout", status=418, content_type="text/plain")
```

Custom headers go alongside:

```python
Response(b"...", headers={"x-request-id": "abc"})
```

`body` may be `bytes` or `str`; a `str` is encoded as UTF-8.

## Status codes you get for free

- `204` when a handler returns `None`
- `404` when nothing matches the path
- `405`, with an `Allow` header, when the path exists for another method
- `413` when the body is over the limit
- `422` when a parameter or body fails validation
- `426` on a plain `GET` to a WebSocket route
- `500` when the handler raises, with no detail in the body
- `503`, with `Retry-After`, when every worker is at its concurrency limit
- `504` when a handler does not respond within `request_timeout`
