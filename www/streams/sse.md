# Server-Sent Events

Return an `SSE` and Aether streams it as `text/event-stream`.

```python
@app.get("/events")
async def events(_: Request):
    return SSE(app.topic("feed").subscribe())
```

The source is any async iterable, so a topic subscription is the common case
but a generator works just as well:

```python
@app.get("/clock")
async def clock(_: Request):
    async def ticks():
        while True:
            yield datetime.datetime.now().isoformat()
            await asyncio.sleep(1)

    return SSE(ticks())
```

## What gets sent

Values are encoded as JSON, or sent as-is if they are already strings. A
pydantic model goes through pydantic's serializer.

Yield an `Event` to set a name, an id for resumption, or a client retry hint:

```python
from aether import Event

yield Event({"price": 42}, event="tick", id="1051", retry=3000)
```

A payload containing newlines becomes several `data:` lines, as the wire format
requires — a raw newline would otherwise end the event early.

## Keep-alive

Idle connections get a comment line every `ping` seconds, 15 by default, so
proxies and load balancers do not close a quiet stream.

```python
SSE(source, ping=30)      # or ping=None to disable
```

## Disconnects

A disconnected client is detected without polling. The response body carries a
guard that hyper drops when the connection ends, and the pump races that
against the next message.

This matters more than it sounds. A stream waiting on a quiet topic has nothing
to write and therefore nothing that would fail, so without the guard it would
never notice its client had left — and would hold its subscription open
forever.

When the client goes away, the source is closed: a topic subscription is
released, and a generator gets its `close`.

!!! note "The concurrency slot is released explicitly"

    A finished stream frees its worker concurrency slot at the moment it
    finishes, not whenever the garbage collector breaks the reference cycle
    holding it. Relying on collection looks deterministic and is not, and the
    resulting leak is invisible under light load. A resource limit is never
    released by garbage collection.
