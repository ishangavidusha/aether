"""A live feed: one publish reaches every subscriber, on every worker loop.

This is the point of building on free-threaded Python. The server runs several
event loops in one process, browsers connect to whichever loop happens to take
their request, and a message published through any of them fans out to all of
them because they share memory. Under a multiprocess server each worker would
hold its own private copy of the topic and this would silently not work.

Run it, open http://127.0.0.1:8000/ in two or three browser tabs, and post:

    curl -X POST 127.0.0.1:8000/say -d '{"who":"ada","text":"hello"}'
"""

import asyncio
import datetime

from pydantic import BaseModel, Field

from aether import SSE, App, Event, Request, Response

app = App(title="Live Feed", version="0.1.0")

# `block` would guarantee delivery but let one stalled browser hold up every
# publisher. A feed would rather drop history for a slow reader.
FEED = "feed"


class Message(BaseModel):
    who: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=500)


@app.post("/say")
async def say(_: Request, body: Message):
    """Publish a message to everyone listening."""
    at = datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")
    reached = await app.topic(FEED).emit(
        Event(data={"who": body.who, "text": body.text, "at": at}, event="message")
    )
    return {"delivered_to": reached}


@app.get("/events")
async def events(_: Request):
    """The SSE stream. One subscription per connected browser."""
    return SSE(app.topic(FEED).subscribe(maxsize=64))


@app.get("/stats")
async def stats(_: Request):
    return {"listeners": app.topic(FEED).subscribers}


@app.get("/clock")
async def clock(_: Request):
    """A stream that is not backed by a topic; any async iterable works."""

    async def ticks():
        while True:
            yield datetime.datetime.now(datetime.UTC).strftime("%H:%M:%S")
            await asyncio.sleep(1)

    return SSE(ticks())


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Live Feed</title>
<style>
 body{font:15px/1.5 system-ui;margin:2rem auto;max-width:34rem}
 li{margin:.2rem 0} .who{font-weight:600} .at{color:#888;font-size:.85em}
 form{display:flex;gap:.4rem;margin:1rem 0}
 input{flex:1;padding:.4rem} button{padding:.4rem .8rem}
</style>
<h1>Live feed <small id="n"></small></h1>
<form onsubmit="send(event)">
  <input id="who" placeholder="name" value="anon" size="8">
  <input id="text" placeholder="say something" autofocus>
  <button>send</button>
</form>
<ul id="log"></ul>
<script>
const log = document.getElementById("log");
new EventSource("/events").addEventListener("message", e => {
  const m = JSON.parse(e.data), li = document.createElement("li");
  li.innerHTML = `<span class="who"></span> <span></span> <span class="at"></span>`;
  li.children[0].textContent = m.who + ":";
  li.children[1].textContent = m.text;
  li.children[2].textContent = m.at;
  log.prepend(li);
});
async function send(e) {
  e.preventDefault();
  const text = document.getElementById("text");
  if (!text.value) return;
  await fetch("/say", {method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({who: document.getElementById("who").value, text: text.value})});
  text.value = "";
}
setInterval(async () => {
  const s = await (await fetch("/stats")).json();
  document.getElementById("n").textContent = `(${s.listeners} listening)`;
}, 2000);
</script>
"""


@app.get("/")
async def index(_: Request):
    return Response(PAGE, content_type="text/html; charset=utf-8")


if __name__ == "__main__":
    app.run(port=8000)
