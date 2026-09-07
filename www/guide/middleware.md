# Middleware

Middleware runs around every HTTP handler.

```python
from aether import Reply

@app.middleware
async def require_key(request, call_next):
    if request.header("x-api-key") != SECRET:
        return Reply({"error": "unauthorized"}, status=401)
    reply = await call_next(request)
    reply.headers["x-served-by"] = "aether"
    return reply
```

Middleware runs outermost-first in registration order and unwinds in reverse.
It can observe a request, add response headers, change the status, or refuse to
call the handler at all.

## `call_next` returns a Reply, not a response

A [`Reply`](../reference/http.md#aether.Reply) holds whatever the handler
returned, still unserialized: a dict, a model, a `Response`, an `SSE`, or
`None`. It is not a finished body.

This is intentional. A handler returning a dict is serialized to JSON in Rust,
and materializing a body so that middleware could inspect it would discard that
on every request, including requests whose middleware never inspects anything.
Middleware that does need the bytes can replace `reply.value` with a `Response`
it builds itself.

```python
@app.middleware
async def timing(request, call_next):
    started = time.perf_counter()
    reply = await call_next(request)
    reply.headers["x-elapsed-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    return reply
```

## Cost

Routes are untouched when no middleware is registered — the chain is built at
startup, not per request, and a route with an empty chain calls the handler
directly.

## WebSocket routes

Middleware does **not** wrap socket handlers. A WebSocket handler runs after the
handshake has already completed, so there is nothing useful left to intercept:
the `101` is sent and the client believes it is connected.

Middleware *does* wrap a socket route's `authorize` function, which runs before
the handshake. That is how an app-wide rule still reaches sockets: give the
route an authorizer, even one that accepts everything, and the middleware chain
wraps it.

```python
async def accept(request):
    return None            # None or True accepts; a Response or Reply refuses

@app.websocket("/feed", authorize=accept)
async def feed(request, ws): ...
```

With `require_key` registered above, that socket is now refused with `401`
before the upgrade, by the same rule that guards the REST routes. A socket
route with no authorizer at all is not wrapped by anything. See
[WebSocket](../streams/websockets.md#refusing-a-connection).
