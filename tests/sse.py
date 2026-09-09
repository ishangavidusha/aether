#!/usr/bin/env python3
"""Server-Sent Events end to end.

The point of this test is the milestone's exit criterion: one emit inside a
handler must reach subscribers living on every worker loop, through real HTTP.
"""
import asyncio
import sys
import threading

import socket
import time

import httpx
from pydantic import BaseModel

from aether import SSE, App, Event, Request

PORT = 8803
BASE = f"http://127.0.0.1:{PORT}"
WORKERS = 4
CLIENTS = 8

app = App(openapi_url=None, docs_url=None)


class Order(BaseModel):
    id: int
    item: str


@app.get("/feed")
async def feed(_: Request):
    return SSE(app.topic("orders").subscribe(), ping=None)


@app.post("/publish/{item}")
async def publish(_: Request, item: str):
    reached = await app.topic("orders").emit(Order(id=1, item=item))
    return {"delivered": reached}


@app.get("/shapes")
async def shapes(_: Request):
    async def source():
        yield "plain text"
        yield {"a": 1}
        yield Order(id=7, item="model")
        yield Event(data="named", event="tick", id="42")
        yield Event(data="line one\nline two")

    return SSE(source(), ping=None)


@app.get("/finite")
async def finite(_: Request):
    async def source():
        for i in range(3):
            yield i

    return SSE(source(), ping=None)


#: Big enough that a few hundred fill the connection's chunk buffer and the
#: socket buffer behind it, which is what makes a slow reader slow.
BULK = "x" * 8000
BULK_EVENTS = 400


@app.get("/bulk")
async def bulk(_: Request):
    async def source():
        for i in range(BULK_EVENTS):
            yield {"n": i, "pad": BULK}

    return SSE(source(), ping=None)


@app.get("/slowping")
async def slowping(_: Request):
    """A source that never yields, so only pings should arrive."""

    async def source():
        await asyncio.sleep(30)
        yield "never"

    return SSE(source(), ping=0.1)


@app.get("/subscribers")
async def subscribers(_: Request):
    return {"count": app.topic("orders").subscribers}


def parse_events(text: str) -> list[dict]:
    """Minimal text/event-stream parser."""
    events, current = [], {"data": []}
    for line in text.split("\n"):
        if line == "":
            if current["data"] or len(current) > 1:
                current["data"] = "\n".join(current["data"])
                events.append(current)
            current = {"data": []}
        elif line.startswith(":"):
            current.setdefault("comments", []).append(line[1:].strip())
        elif ":" in line:
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data":
                current["data"].append(value)
            else:
                current[field] = value
    return events


async def read_n_events(client, n, timeout=15):
    """Open a stream, collect n events, return them."""
    out = []
    async with client.stream("GET", "/feed", timeout=timeout) as r:
        if r.status_code != 200:
            return {"error": f"status {r.status_code}"}
        ctype = r.headers.get("content-type", "")
        buffer = ""
        async for chunk in r.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, _, buffer = buffer.partition("\n\n")
                out.extend(parse_events(raw + "\n\n"))
                if len(out) >= n:
                    return {"events": out, "content_type": ctype,
                            "cache_control": r.headers.get("cache-control")}
    return {"events": out, "content_type": ctype}


async def run() -> list[str]:
    bad: list[str] = []
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
        # Fan-out across worker loops. More clients than workers, so several
        # loops each hold more than one subscription.
        readers = [asyncio.create_task(read_n_events(client, 2)) for _ in range(CLIENTS)]

        for _ in range(100):
            n = (await client.get("/subscribers")).json()["count"]
            if n == CLIENTS:
                break
            await asyncio.sleep(0.05)
        else:
            bad.append(f"only {n} of {CLIENTS} subscriptions registered")

        first = (await client.post("/publish/widget")).json()
        if first["delivered"] != CLIENTS:
            bad.append(f"first emit reached {first['delivered']} of {CLIENTS}")
        await client.post("/publish/gadget")

        results = await asyncio.gather(*readers)
        for i, res in enumerate(results):
            if "error" in res:
                bad.append(f"client {i}: {res['error']}")
                continue
            events = res.get("events", [])
            if len(events) < 2:
                bad.append(f"client {i} got {len(events)} events, expected 2")
                continue
            if events[0]["data"] != '{"id":1,"item":"widget"}':
                bad.append(f"client {i} first payload was {events[0]['data']!r}")
            if events[1]["data"] != '{"id":1,"item":"gadget"}':
                bad.append(f"client {i} second payload was {events[1]['data']!r}")
        if results and "content_type" in results[0]:
            if "text/event-stream" not in results[0]["content_type"]:
                bad.append(f"content-type was {results[0]['content_type']!r}")
            if results[0].get("cache_control") != "no-cache":
                bad.append(f"cache-control was {results[0].get('cache_control')!r}")

        # Subscriptions must be released when the client disconnects.
        for _ in range(100):
            if (await client.get("/subscribers")).json()["count"] == 0:
                break
            await asyncio.sleep(0.05)
        else:
            left = (await client.get("/subscribers")).json()["count"]
            bad.append(f"{left} subscriptions leaked after clients disconnected")

        # Payload shapes.
        r = await client.get("/shapes")
        events = parse_events(r.text)
        want = [
            {"data": "plain text"},
            {"data": '{"a":1}'},
            {"data": '{"id":7,"item":"model"}'},
            {"data": "named", "event": "tick", "id": "42"},
            {"data": "line one\nline two"},
        ]
        if len(events) != len(want):
            bad.append(f"/shapes produced {len(events)} events, expected {len(want)}")
        else:
            for i, (got, exp) in enumerate(zip(events, want)):
                for key, value in exp.items():
                    if got.get(key) != value:
                        bad.append(f"/shapes event {i}: {key}={got.get(key)!r}, expected {value!r}")

        # A finite source must end the response cleanly.
        r = await client.get("/finite")
        events = parse_events(r.text)
        if [e["data"] for e in events] != ["0", "1", "2"]:
            bad.append(f"/finite produced {[e['data'] for e in events]}")

        # An idle stream must emit keep-alive comments, and must be torn down
        # when the client walks away mid-stream.
        pings = 0
        async with client.stream("GET", "/slowping", timeout=10) as r:
            async for chunk in r.aiter_text():
                pings += chunk.count(": ping")
                if pings >= 3:
                    break
        if pings < 3:
            bad.append(f"idle stream produced {pings} pings, expected at least 3")

    bad.extend(slow_client_loses_nothing())
    return bad


def slow_client_loses_nothing() -> list[str]:
    """A reader slower than the source must not silently miss events.

    `send_chunk` cannot block, so when the connection's buffer is full it
    reports that and returns. The pump used to ignore the report: this exact
    case delivered 148 of 400 events, with nothing in the stream or the log to
    say the other 252 had ever existed. An event stream that quietly drops
    events is worse than one that stalls, and stalling is what the topic
    policies are for.

    Raw sockets on purpose: an HTTP client reads as fast as it can, which is
    the one thing this must not do.
    """
    bad: list[str] = []
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=20)
    try:
        sock.sendall(b"GET /bulk HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        # Read nothing while the source runs flat out.
        time.sleep(1.5)
        sock.settimeout(10.0)
        data = b""
        while True:
            block = sock.recv(1 << 20)
            if not block:
                break
            data += block
    except socket.timeout:
        bad.append("the bulk stream never finished")
    finally:
        sock.close()

    seen = data.count(b'"n":')
    if seen != BULK_EVENTS:
        bad.append(f"a slow client received {seen} of {BULK_EVENTS} events")
    return bad


def main() -> None:
    threading.Thread(target=lambda: app.run(port=PORT, workers=WORKERS), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/subscribers", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    failures = asyncio.run(run())
    print(f"workers          : {WORKERS} loops, {CLIENTS} concurrent SSE clients")
    print(f"checks           : {'PASS' if not failures else 'FAIL'}")
    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
