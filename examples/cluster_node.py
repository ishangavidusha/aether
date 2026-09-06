"""One node of a multi-process Aether cluster. See docker-compose.stack.yml.

Every node subscribes to the same durable topic. Publish to any node and all
of them deliver it, because the topic is backed by a Redis stream and each
node tails it. This is the claim that a single-process test cannot make.

    make stack
    curl -X POST 127.0.0.1:8001/say -d '{"text":"hello"}'
    curl 127.0.0.1:8002/seen       # node B saw what node A published
"""

import asyncio
import os

from pydantic import BaseModel, Field

from aether import SSE, App, Request

NODE = os.environ.get("AETHER_NODE", "node")
REDIS = os.environ.get("AETHER_REDIS", "redis://127.0.0.1:6399")
TOPIC = "cluster"

app = App(title=f"Aether {NODE}", version="0.1.0", redis_url=REDIS)

seen: list[dict] = []
_collector: asyncio.Task | None = None


class Say(BaseModel):
    text: str = Field(min_length=1)


def start_collector() -> None:
    """Record everything this node receives, wherever it was published."""
    global _collector
    if _collector is not None and not _collector.done():
        return

    async def collect():
        async with app.topic(TOPIC, durable=True).subscribe() as sub:
            async for message in sub:
                seen.append(message)

    _collector = asyncio.get_running_loop().create_task(collect())


@app.get("/health")
async def health(_: Request):
    start_collector()
    return {"node": NODE, "ok": True}


@app.post("/say")
async def say(_: Request, body: Say):
    start_collector()
    await app.topic(TOPIC, durable=True).emit({"text": body.text, "from": NODE})
    return {"published_by": NODE}


@app.get("/seen")
async def seen_here(_: Request):
    start_collector()
    return {"node": NODE, "count": len(seen), "messages": seen[-10:]}


@app.get("/events")
async def events(_: Request):
    start_collector()
    return SSE(app.topic(TOPIC, durable=True).subscribe())


if __name__ == "__main__":
    # 0.0.0.0, not loopback: a container's loopback is not reachable from
    # outside it, so binding to 127.0.0.1 would publish a port to nowhere.
    app.run(host="0.0.0.0", port=8000)
