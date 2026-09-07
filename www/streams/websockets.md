# WebSocket

```python
@app.websocket("/ws")
async def echo(request, ws):
    async for message in ws:
        await ws.send(message)
```

Aether performs the handshake, so the socket is already open when the handler
runs, and the connection closes when the handler returns.

The handler takes the request and the socket. Path and query parameters work
exactly as they do on an ordinary route:

```python
@app.websocket("/rooms/{room}")
async def room(request, ws, room: str, verbose: bool = False):
    ...
```

## Sending and receiving

Text arrives as `str`, binary as `bytes`. Sending follows the value rather than
its class:

| sent value | frame |
|---|---|
| `str` | text |
| `bytes` | binary |
| anything else — dicts, pydantic models | JSON, in a text frame |

```python
await ws.send("hello")
await ws.send(b"\x00\x01")
await ws.send({"type": "tick"})
await ws.send_json(payload)          # explicit
message = await ws.receive()         # None when the peer closed
data = await ws.receive_json()
await ws.close()
```

Ping and Pong are answered underneath and never reach the handler.

## Refusing a connection

`authorize` runs **before** the handshake, which the handler cannot do: by the
time a handler runs, the `101` has been sent and the client believes it is
connected.

```python
async def members_only(request):
    if not valid(request.header("authorization")):
        return Response(b"nope", status=401, content_type="text/plain")

@app.websocket("/feed", authorize=members_only)
async def feed(request, ws): ...
```

Return `None` or `True` to accept. Return a `Response` or a `Reply` to refuse
with exactly that. Return anything else falsy and the client gets `403`. The
authorizer may be sync or async, and [middleware wraps
it](../guide/middleware.md#websocket-routes).

## Disconnects cancel the handler

A handler is cancelled when its peer goes away. That matters for the common
pattern of a socket fed by a topic:

```python
@app.websocket("/feed")
async def feed(request, ws):
    async with app.topic("orders").subscribe() as sub:
        async for order in sub:
            await ws.send(order)
```

That handler is blocked on the topic, not on the socket, so it has no way to
notice the browser closed. Cancelling it unwinds the `async with`, which
releases the subscription. Without that, every closed tab would leak a
subscription until the next message happened to arrive.

## Other details

A plain `GET` to a socket route returns `426`. Socket routes are left out of
the OpenAPI document, because OpenAPI 3.1 has no vocabulary for them.

`examples/live_feed.py` is a working chat page serving one topic over both SSE
and WebSocket: open it in several tabs and post a message.
