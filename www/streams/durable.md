# Durable topics

A topic backed by a Redis stream persists, replays, and reaches subscribers in
other processes.

```python
app = App(redis_url="redis://localhost:6379")

@app.post("/orders")
async def place(_: Request, body: Order):
    # Returning means Redis has it, not just this process.
    await app.topic("orders", durable=True).emit(body)
    return {"ok": True}
```

Redis is optional. Aether imports and runs without it; only durable topics need
it. Run it in a container:

```bash
make up
```

## What durability adds

**Cross-process fan-out.** A message emitted in one process reaches subscribers
in another. Local subscribers are still served directly, so they do not wait
for a round trip, and a tail task in every *other* process feeds its subscribers
from the stream. The tail skips messages its own node published, so nobody sees
a message twice.

**Replay.** Messages live in the stream, so a subscriber can start from an
earlier position rather than only seeing what arrives next.

```python
recent = await app.topic("orders", durable=True).history(count=50)
```

**At-least-once delivery**, through consumer groups.

## Consumer groups

For work that must not be lost. Each message goes to exactly one member of the
group and stays pending until acknowledged.

```python
topic = app.topic("orders", durable=True)

async with topic.consumer("billing", "worker-1") as c:
    async for message in c:
        await charge(message.data)
        await message.ack()          # only now is it done
```

Kill that worker mid-message and the message comes back when it restarts,
because it was never acknowledged. If it never restarts, another member claims
it after `claim_after_ms` (60s by default).

```python
topic.consumer("billing", "worker-1", claim_after_ms=30_000, count=64)
```

That is at-least-once, not exactly-once: a worker that crashes *after* charging
and *before* acknowledging will see the message again. Make the work
idempotent.

`examples/durable_queue.py` demonstrates it. Killing the server with eight jobs
unacknowledged recovered all eight.

## Emitting is an await

`emit_nowait` is **refused** on a durable topic. Appending to a stream is an
await, so a synchronous call could only satisfy the signature by skipping
durability, which the name does not admit.

Emitting while Redis is down raises, for the same reason: a publish that
returned successfully without persisting anything is the failure mode durable
topics exist to prevent.

## Outages

A dropped connection is retried with backoff, and the tail task reconnects
rather than dying silently — a tail that stopped without saying so would leave
a node quietly deaf to every other node. One `WARNING` per outage, one line
when it comes back.

## Trimming

Streams are trimmed to roughly 10,000 entries. Redis trims approximately, on
whole nodes, so the real length hovers a little above that.

## Two nodes, one Redis

```bash
make stack
```

Builds the app image and runs two nodes against one Redis, which is the only
way to exercise cross-process fan-out the way it actually ships.
