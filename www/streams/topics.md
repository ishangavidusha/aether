# Topics

A topic is a named fan-out point inside the process. Producers emit,
subscribers iterate.

```python
@app.post("/say")
async def say(_: Request, body: Message):
    return {"delivered_to": await app.topic("feed").emit(body)}

@app.get("/events")
async def events(_: Request):
    return SSE(app.topic("feed").subscribe())
```

`app.topic(name)` gets or creates one. The same name always returns the same
object, whichever worker loop asks.

## Every worker loop sees every message

This is the whole reason Aether targets free-threaded Python.

The server runs several event loops in one process. Clients land on whichever
loop happened to take their request, so two subscribers to the same feed are
usually on different loops. Because those loops share memory, a message
published through any of them reaches all of them.

Under a multiprocess server — the usual way to get parallelism out of CPython —
each worker holds a private copy of every topic, and this quietly does not
work: half your subscribers never see the message, and nothing reports an
error. That failure mode is why the free-threaded build is the primary target
rather than a curiosity.

## Emitting

```python
count = await topic.emit(value)     # returns how many subscribers took it
topic.emit_nowait(value)            # from sync code, in-process topics only
```

`emit` accepts anything: a dict, a pydantic model, a string. Subscribers get
the object itself, not a copy and not a serialization — this is a queue in one
process, so nothing is encoded until something actually sends it somewhere.

## Subscribing

```python
async with topic.subscribe() as sub:
    async for item in sub:
        ...
```

Use the context manager. It releases the subscription when the block exits,
including when the handler is cancelled because its client disconnected.
Without it, every closed browser tab leaks a subscription until the process
restarts.

`subscribe()` also works bare, as an async iterator, which is what allows
`SSE(topic.subscribe())`: the SSE machinery closes the subscription itself.

## Backpressure

Each subscriber has its own buffer, `maxsize` deep. What happens when it fills
is the topic's policy, overridable per subscription:

| policy | when a subscriber's buffer is full |
|---|---|
| `drop_oldest` | discard the oldest buffered message. **The default** |
| `drop_newest` | discard the message being emitted |
| `block` | the producer waits for room, guaranteeing delivery |
| `error` | raise `TopicFull` at the producer |

```python
from aether import BLOCK, DROP_NEWEST

app.topic("orders", maxsize=4096, policy=BLOCK)     # at creation
topic.subscribe(maxsize=16, policy=DROP_NEWEST)     # for one subscriber
```

`drop_oldest` is the default because a live feed loses history for one slow
reader rather than stalling every producer in the process. Choose `block` when
delivery matters more than latency, keeping in mind that one stalled subscriber
then holds up every producer.

Policies govern buffering, not persistence. For messages that must survive a
restart, see [durable topics](durable.md).

## Why topics are Python and not Rust

The rest of the hot path is Rust because tokio threads must never touch the
interpreter. That argument does not apply here. Both ends of a topic are
already Python, so moving the buffer into Rust would add a foreign-function
crossing on emit *and* on receive, to replace a `deque` operation that is
cheaper than either crossing.

Waking a subscriber costs anything at all only when it is idle, so a busy
stream coalesces naturally — the same principle that made request dispatch
fast.
