# Aether

A fast Python REST framework with a Rust core, built-in reactive streams, and
agent-native interfaces. Hobby project, not a product.

**Status: milestones 1-5 complete.** Routing, typed path and query parameters,
pydantic bodies, backpressure, OpenAPI 3.1, in-process topics, Server-Sent
Events and WebSocket all work. Nothing here is API-stable.

## Design decisions so far

- Rust runtime (tokio + hyper + PyO3). The developer-facing API is Python.
- Handlers are `async def` only.
- Streams will be in-memory pub/sub by default with an optional Redis Streams layer.
- Free-threaded Python 3.14 (`python3.14t`) is the primary target. The GIL build
  still works, with a single Python worker loop.
- Worker loops default to the detected parallelism, capped at 8, and to exactly
  one on GIL builds.
- Long term: one handler declaration produces a REST route, an OpenAPI entry,
  an MCP tool, and an agent-callable capability. No new wire protocol yet.

## Layout

```
src/            Rust crate, built as the `aether._core` extension module
  server.rs     tokio accept loop, hyper HTTP/1.1, enqueue
  router.rs     per-method radix trees, path and query params coerced in Rust
  queue.rs      bounded per-worker queue + socketpair wakeup
  responder.rs  reply channel, streaming bodies, disconnect signal
  websocket.rs  upgrade bridge over tokio-tungstenite
  worker.rs     one OS thread + one asyncio loop per worker; drain callback
  request.rs    frozen Request pyclass handed to handlers
  responder.rs  one-shot reply channel; JSON is serialized in Rust
python/aether/  App, routing, pydantic, OpenAPI, topics, SSE, sockets, runtime
examples/       hello.py
bench/          baseline apps, hello-world runner, CPU and handler-cost sweeps
tests/          dispatch, routing, query, bodies, openapi, streams, sse, ...
```

**Request path.** A tokio thread parses the request, matches it against a radix
tree per method, coerces its path and query parameters, and pushes a plain Rust struct onto
the chosen worker's lock-free queue. It never attaches to the interpreter. If no
wakeup is already in flight it writes a single byte to a socketpair that the
worker's asyncio loop watches via `add_reader`.

On the worker thread, a native drain callback consumes the byte, pops every
queued request, and schedules `run_handler` for each. The handler's return value
is serialized to JSON in Rust and sent back to the waiting tokio task over a
oneshot channel.

Two properties matter. Python is only ever touched from the worker's own
thread, and a burst of requests collapses into one wakeup instead of one per
request.

## Setup

Requires Rust, `uv`, Docker for services, and `oha` (`brew install oha`) for
benchmarks. Nothing Aether depends on is installed on the host.

```bash
make up             # Redis in a container, for durable topics
make down           # stop it
make stack          # build the image, run two nodes against one Redis
```

Without `make up`, the durable tests print SKIP and pass, so start it before
trusting a green run.

```bash
make venvs          # creates .venv (3.14t) and .venv-gil (3.14), installs deps
make build          # maturin develop --release into both venvs
make run            # examples/hello.py on the free-threaded build
make verify         # all seven test suites
make bench          # hello-world comparison, free-threaded
make bench-gil      # hello-world comparison, GIL build
make bench-cpu      # CPU-bound handler scaling, free-threaded
make bench-cpu-gil  # CPU-bound handler scaling, GIL build
make sweep          # handler cost vs worker loops, free-threaded
make sweep-gil      # handler cost vs worker loops, GIL build
```

Hello world:

```python
from aether import App, Request

app = App()

@app.get("/")
async def hello(_: Request):
    return {"hello": "world"}

app.run(port=8000)
```

## Routing

Path parameters are declared in the path and typed by the handler's
annotations. `{*name}` captures the rest of the path.

```python
@app.get("/users/{user_id}")
async def get_user(_: Request, user_id: int):
    return {"user_id": user_id}

@app.get("/files/{*rest}")
async def get_file(_: Request, rest: str):
    return {"path": rest}
```

Coercion happens in Rust, on the same thread that parsed the request, so a bad
path never wakes a Python worker:

```
GET /users/42    ->  200  {"user_id": 42}
GET /users/abc   ->  422  {"detail": [{"type": "path_param_parsing", ...}]}
DELETE /users/42 ->  405  Allow: GET
```

Supported parameter types are `str`, `int`, `float` and `bool`. Anything else is
a `TypeError` at import time, as is a path parameter the handler does not
accept.

## Query parameters

Any handler argument that is not a path parameter and not a pydantic model is a
query parameter, coerced in Rust alongside path parameters.

```python
@app.get("/search")
async def search(_: Request, q: str, limit: int = 10, cursor: str | None = None):
    return {"q": q, "limit": limit, "cursor": cursor}
```

- No default means required. A missing one returns 422 before Python is woken.
- A default makes it optional, and the handler's own default applies.
- `str | None` without a default is optional and arrives as `None`.

Booleans accept `true/false`, `1/0`, `yes/no` and `on/off`. A repeated key uses
the first value; lists are not supported yet. Routes that declare no query
parameters skip query-string parsing entirely.

A typed path parameter costs nothing measurable:

| target | req/s |
|---|---:|
| aether `/` | 184,861 |
| aether `/users/{user_id}` | 184,697 |
| aether `/search?q=..&limit=..` | 181,478 |
| granian + fastapi query route | 19,478 |
| uvicorn + fastapi query route | 9,851 |

Routes with no parameters skip the parameter dict entirely, which is why the
hello-world number did not move when routing landed.

## Request bodies

An argument annotated with a pydantic model binds the request body. Returning a
model serializes it, and only the fields that model declares are sent.

```python
from pydantic import BaseModel

class UserIn(BaseModel):
    name: str
    age: int

class UserOut(BaseModel):
    id: int
    name: str

@app.post("/users")
async def create_user(_: Request, body: UserIn):
    return UserOut(id=1, name=body.name)
```

A body that fails validation returns 422 carrying pydantic's own errors, in the
same `{"detail": [...]}` shape as a path parameter failure, so a client parses
one format for every 422.

Validation runs on the worker thread rather than in Rust, which is the one place
Aether wakes Python before rejecting bad input. It costs about 13%:

| target | req/s | vs aether |
|---|---:|---:|
| aether hello world | 184,861 | 1.0x |
| aether validated POST | 158,031 | 1.2x |
| granian + fastapi validated POST | 17,023 | 10.9x |
| uvicorn + fastapi validated POST | 9,948 | 18.6x |

Both sides run the same pydantic version on the same models, so that gap is
dispatch and serialization, not validation.

Pydantic is a dependency, but Aether imports and runs without it. Only body
models need it.

## OpenAPI

The schema is generated from the same route metadata the router uses, so it
cannot describe an endpoint the server would not accept.

```python
app = App(title="My API", version="1.0.0")
```

`/openapi.json` and `/docs` are served automatically; pass `openapi_url=None` or
`docs_url=None` to turn either off. `app.openapi()` returns the document without
starting a server, which makes it usable for client generation in CI.

Path and query parameters, request and response models, docstring summaries and
the 422 shape all appear in the document. Nested models are hoisted into
`components/schemas`. The output is checked against `openapi-spec-validator` in
the test suite, so "valid OpenAPI 3.1" is verified rather than assumed.

## Streams

A topic is a named fan-out point. Producers emit, subscribers iterate.

```python
@app.post("/say")
async def say(_: Request, body: Message):
    return {"delivered_to": await app.topic("feed").emit(body)}

@app.get("/events")
async def events(_: Request):
    return SSE(app.topic("feed").subscribe())
```

**Subscribers on different worker loops all receive every message.** That is the
whole reason for targeting free-threaded Python. The server runs several event
loops in one process, clients land on whichever loop takes their request, and a
message published through any of them reaches all of them because they share
memory. Under a multiprocess server each worker would hold a private copy of the
topic and this would silently not work.

Backpressure is per topic or per subscription:

| policy | when a subscriber's buffer is full |
|---|---|
| `drop_oldest` | discard the oldest buffered message. The default |
| `drop_newest` | discard the message being emitted |
| `block` | producer waits for room, guaranteeing delivery |
| `error` | raise `TopicFull` at the producer |

`drop_oldest` is the default because a feed would rather lose history for one
slow reader than stall every producer.

Topics are plain Python, not Rust. Both ends are already Python, so a Rust
buffer would add a foreign-function crossing on emit and on receive to replace a
deque operation cheaper than either crossing. Waking a subscriber costs anything
at all only when it is idle, so a busy stream coalesces naturally.

## Agents

The same handler that serves HTTP can be a capability an agent calls, over the
Model Context Protocol. Nothing is declared twice: the tool's name, description,
argument schema and output schema all come from the handler that already exists.

```python
@app.get("/notes/{note_id}", tool=True)
async def read_note(_: Request, note_id: int) -> Note:
    """Read one note by its id."""
    return Note(**NOTES[note_id])
```

Point an MCP client at `/mcp` and it sees `read_note` with a typed `note_id`
argument, that docstring as its description, `Note` as its output schema, and a
read-only hint inferred from the fact that it is a GET.

**`tool=True` is opt-in on purpose.** A route without it is still a perfectly
good endpoint; it simply is not offered to agents. Every route being
agent-callable by default would mean an administrative delete endpoint is
agent-callable by default.

Body fields are flattened into the argument list, so an agent calls
`write_note(title=..., body=...)` rather than nesting an object whose shape it
has to infer. A body field that collides with a path or query parameter is an
error at import time.

Topics show up as readable resources at `topic://<name>`; a durable one returns
recent messages.

The transport is the simple half of the spec: a POST carrying one JSON-RPC
message, answered with JSON. Streaming responses and the server-to-client GET
channel are not implemented, and the router returns 405 for them, which is what
the spec asks. `tests/capabilities.py` drives the official MCP SDK client
against a running server.

`examples/agent_service.py` serves one set of declarations to curl, to an
OpenAPI client and to an agent.

## Durable topics

A topic backed by a Redis stream persists, replays, and reaches subscribers in
other processes.

```python
app = App(redis_url="redis://localhost:6399")

@app.post("/orders")
async def place(_: Request, body: Order):
    # Returning means Redis has it, not just this process.
    await app.topic("orders", durable=True).emit(body)
    return {"ok": True}
```

Local subscribers are still served directly, so they do not wait for a round
trip. A tail task in every other process feeds its subscribers from the stream
and skips messages its own node published, so nobody sees a message twice.

For work that must not be lost, use a consumer group. Each message goes to
exactly one member and stays pending until acknowledged:

```python
async with app.topic("orders", durable=True).consumer("billing", "worker-1") as c:
    async for message in c:
        await charge(message.data)
        await message.ack()      # only now is it done
```

Kill that worker mid-message and the message comes back when it restarts, or
another member claims it after `claim_after_ms`. `examples/durable_queue.py`
demonstrates it; killing the server with 8 jobs unacknowledged recovered all 8.

`emit_nowait` is refused on a durable topic, because appending is an await and
a call named emit that silently skipped durability would be worse than an
error. Emitting while Redis is down raises, for the same reason.

## Server-Sent Events

Return an `SSE` and Aether streams it. The source is any async iterable, so a
topic subscription is the common case but a generator works too.

```python
@app.get("/clock")
async def clock(_: Request):
    async def ticks():
        while True:
            yield datetime.datetime.now().isoformat()
            await asyncio.sleep(1)
    return SSE(ticks())
```

Values are encoded as JSON, or sent as-is if they are already strings. Yield an
`Event` to set a name, an id, or a client retry hint. Idle connections get a
keep-alive comment every `ping` seconds, 15 by default, so proxies do not close
them.

A disconnected client is detected without polling. The response body carries a
guard that hyper drops when the connection ends, and the pump races that against
the next message. Without it a stream waiting on a quiet topic would never
notice its client had left, leaking the subscription indefinitely.

## WebSocket

```python
@app.websocket("/ws")
async def echo(request, ws):
    async for message in ws:
        await ws.send(message)
```

Aether does the handshake, so the socket is already open when the handler runs.
Text arrives as `str` and binary as `bytes`. Sending follows the value rather
than its class: `str` goes as text, `bytes` as binary, and everything else,
dicts and pydantic models alike, as JSON in a text frame. Ping and Pong are
answered underneath and never reach the handler. Path and query parameters work
exactly as they do on an ordinary route.

**A handler is cancelled when its peer disconnects.** That matters for the
pattern this framework is built around:

```python
@app.websocket("/feed")
async def feed(request, ws):
    async with app.topic("orders").subscribe() as sub:
        async for order in sub:
            await ws.send(order)
```

That handler is blocked on the topic, not the socket, so it has no way to notice
the browser closed. Cancelling unwinds the `async with`, which releases the
subscription. Without it, every closed tab would leak a subscription until the
next message happened to arrive.

A plain `GET` to a socket route returns 426. Socket routes are left out of the
OpenAPI document, since OpenAPI 3.1 has no vocabulary for them.

`examples/live_feed.py` is a working chat page serving the same topic over both
transports: open it in several tabs and post a message.

## Explicit responses

Return a `Response` when you need a specific status code or content type.

```python
from aether import Response

@app.get("/teapot")
async def teapot(_: Request):
    return Response(b"short and stout", status=418, content_type="text/plain")
```

Custom headers go in `headers`:

```python
Response(b"...", headers={"x-request-id": "abc"})
```

## Backpressure

Each worker accepts at most `max_concurrency` requests at once, counting both
queued and in-flight. Beyond that the request tries another worker, and if every
worker is full the server answers 503 with `Retry-After`.

```python
app.run(max_concurrency=1024)   # the default, per worker loop
```

Counting in-flight requests is the part that matters. The drain callback empties
the queue into asyncio tasks immediately, so a handler that awaits I/O leaves
the queue near empty while requests pile up inside the loop. Bounding the queue
alone would look like backpressure and protect nothing.

Lower it for slow handlers, where a deep backlog only adds latency before an
inevitable client timeout. Raise it to absorb larger bursts of fast requests.

## Benchmark method

`bench/run.py` starts each target as a subprocess, warms it up for 2s, then
measures with `oha` at 64 connections for 8s. `raw` targets are a bare ASGI
callable returning a pre-encoded body (best case for the server). `Nw` targets
use one worker per CPU. Results land in `bench/results/*.json`.

Machine: Apple Silicon, 10 cores (4 performance). Numbers below are from the
first run on 2026-09-06 and will move as the dispatch design changes.

### Hello world, free-threaded Python 3.14.7

| target            | req/s   | p50 ms | p99 ms |
|-------------------|--------:|-------:|-------:|
| aether 1 loop     | 189,015 |   0.33 |   0.55 |
| aether 2 loops    | 198,904 |   0.31 |   0.66 |
| aether 4 loops    | 197,358 |   0.27 |   1.24 |
| aether 10 loops   | 171,304 |   0.28 |   1.96 |

Four loops is the current default on this machine. See "Choosing worker loops".
| uvicorn raw       |  19,493 |   3.30 |   3.42 |
| uvicorn raw 10w   |  66,474 |   0.65 |   4.61 |
| uvicorn fastapi   |  12,411 |   5.17 |   5.36 |
| granian raw       | 137,877 |   0.46 |   0.72 |
| granian raw 10w   | 123,479 |   0.28 |   1.99 |
| granian fastapi   |  29,674 |   2.14 |   2.51 |

### Hello world, GIL Python 3.14.7

| target            | req/s   | p50 ms | p99 ms |
|-------------------|--------:|-------:|-------:|
| aether 1 loop     | 192,054 |   0.33 |   0.54 |
| aether 2 loops    | 186,449 |   0.32 |   0.72 |
| aether 4 loops    | 176,079 |   0.33 |   0.90 |
| aether 8 loops    | 138,108 |   0.42 |   1.22 |
| uvicorn raw       |  18,116 |   3.53 |   3.80 |
| uvicorn raw 10w   |  62,251 |   0.40 |   8.03 |
| uvicorn fastapi   |  11,572 |   5.55 |   5.72 |
| granian raw       | 128,733 |   0.50 |   0.78 |
| granian raw 10w   | 117,680 |   0.32 |   3.76 |
| granian fastapi   |  35,238 |   1.79 |   2.20 |

### Dispatch rewrite, before and after

The first design woke a worker with `loop.call_soon_threadsafe` per request and
built `Request`/`Responder` on the tokio thread. The queue design does neither.

| build / loops        | call_soon | queue   | change |
|----------------------|----------:|--------:|-------:|
| free-threaded, 1     |    35,158 | 189,015 |  5.4x  |
| free-threaded, 10    |    14,114 | 171,304 | 12.1x  |
| GIL, 1               |    63,163 | 192,054 |  3.0x  |

### CPU-bound handler, scaling with worker loops

`bench/cpu.py`, 32 connections, a 20,000-iteration Python loop per request.
This is the free-threading thesis under test: can one process run Python
handlers in parallel?

| loops | free-threaded req/s | speedup | GIL req/s | speedup |
|------:|--------------------:|--------:|----------:|--------:|
|     1 |               2,659 |   1.00x |     2,308 |   1.00x |
|     2 |               5,205 |   1.96x |     2,372 |   1.03x |
|     4 |               8,380 |   3.15x |     2,375 |   1.03x |
|     8 |               7,201 |   2.71x |     2,357 |   1.02x |

## Findings from the spike

1. **Dispatch was the entire bottleneck, not the language boundary.** Removing
   per-request Python work from tokio threads gave 5.4x on one loop and 12.1x on
   ten. The original design's cost was `call_soon_threadsafe` plus constructing
   two pyclass objects while attached, paid once per request on a thread that
   had no other reason to touch the interpreter.
2. **Aether now leads every baseline on hello world**: about 190k req/s against
   138k for Granian on raw ASGI, 19k for uvicorn, and 12k for FastAPI. Rust-side
   JSON encoding is included in that number.
3. **Free-threading delivers on CPU-bound handlers.** Four loops ran 3.15x the
   throughput of one on 3.14t. The same test on the GIL build is flat at 1.03x
   no matter how many loops run. This is the result that justifies targeting
   3.14t: real parallel handler execution in a single process, which is what
   keeps in-memory streams viable without forcing Redis as a hard dependency.
4. **Scaling stops at the performance cores.** Eight loops were slower than four
   on a machine with four performance cores. Efficiency cores hurt more than
   they help here.
5. **One worker loop is never the right default on free-threaded builds.** A
   sweep of handler cost against loop count (below) found no crossover: extra
   loops win at every handler cost, including a handler that does nothing.
6. **`os.cpu_count()` was the wrong default** and has been replaced. It counts
   efficiency cores, where loops lose throughput rather than add it, and inside
   a container it reports the host's cores rather than the cgroup limit.

## Choosing worker loops

`bench/sweep.py` varies handler CPU cost and loop count together. Handler cost
is calibrated per interpreter, about 20.2ns per loop iteration on this machine.
64 connections, 6s per point, free-threaded 3.14.7.

| handler µs | 1 loop | 2 loops | 4 loops | 8 loops | best |
|---:|---:|---:|---:|---:|:--:|
| 0 | 184,539 | 190,349 | 193,172 | 173,880 | 4L |
| 10 | 95,933 | 116,977 | 114,443 | 137,985 | 8L |
| 50 | 19,159 | 43,052 | 53,605 | 47,497 | 4L |
| 100 | 10,127 | 20,946 | 30,461 | 27,205 | 4L |
| 500 | 2,154 | 4,202 | 7,347 | 6,031 | 4L |

Four loops, the performance-core count here, was best or within a few percent at
every handler cost, worst case 83% of the best result. One loop falls to 29-36%
of achievable once a handler does real work. The same sweep on the GIL build is
flat and extra loops only cost throughput.

So the default is **the detected parallelism, capped at 8, and exactly 1 on GIL
builds**. `python/aether/_workers.py` takes the most constrained answer from
performance cores, physical cores, cgroup CPU quota and available CPUs, because
the target is cores that can genuinely run Python at the same time. Override it
with `app.run(workers=N)`.

One clarification the sweep does not cover: awaiting I/O does not need more
loops. An await yields the loop, so one loop can hold thousands of them. What
needs more loops is CPU time spent inside the handler.

## Correctness

`tests/verify.py` drives 5,000 concurrent requests, each carrying a unique
token, and asserts every response returns its own token. This is the check that
matters for queue dispatch, where the plausible bug is a reply delivered to the
wrong request. It passes on both builds, with the handler invoked exactly 5,000
times and all worker loops used.

```bash
make verify
```

## Errors and limits

A handler that raises returns 500 with no detail. The traceback goes to the
server log; exception messages routinely carry connection strings, file paths
and user data, none of which belongs in an HTTP response. During development:

```python
app = App(debug=True)   # include the exception in the 500 body
```

Request bodies are capped at 16 MiB, and anything larger is answered 413 without
being buffered:

```python
app.run(max_body=64 * 1024 * 1024)
```

`HEAD` is answered wherever `GET` is, returning the headers a `GET` would,
including the `Content-Length` it would have produced, with no body.

## Known gaps

- No request timeouts, so a slow client can hold a connection open.
- No cap on accepted connections; `max_concurrency` bounds handler slots, not
  sockets.
- Shutdown drops in-flight requests instead of draining them.
- A WebSocket cannot be rejected before the handshake completes, so there is no
  auth hook.
- No cookies, middleware, auth or sessions.
- Query parameters cannot be lists; a repeated key uses the first value.
- No `UUID` or date parameter types yet.
- No TLS or HTTP/2; expects a terminating proxy in front.

## Open questions

- Does the single-drain-callback design hold up with slow handlers, where one
  loop's queue backs up while others idle? Round-robin assignment is naive;
  least-loaded may be needed.
- The worker cap of 8 is a guard, not a measured limit. The sweep ran on a
  machine with four performance cores, so it cannot say whether scaling
  continues past 8 on a large homogeneous server.
- Benchmark numbers here were taken at a 1-minute load average of 3.0, decaying
  from earlier runs, so they sit roughly 4% below a quiet machine. Ratios within
  the run are unaffected.
- Where do streams attach? A per-worker loop means an in-memory topic is shared
  across loops in one process, which is the design the free-threading result
  makes possible.
