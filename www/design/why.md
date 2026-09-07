# Why Aether is built this way

Each decision below was settled by a benchmark or by a test, several of them
against the expected outcome. The approaches that were tried and replaced are
documented at the end.

## Rust for the hot path, Python for the API

The developer-facing API is Python because handlers are what the framework
exists to run. The HTTP layer is Rust — tokio, hyper, PyO3 — because parsing,
routing and parameter coercion are precisely the work that does not need an
interpreter.

The line between them is one rule: **tokio threads never touch the
interpreter.** No attaching, no object creation, no refcount. Requests cross
into Python as plain Rust structs pushed onto a queue.

That rule was worth 5.4x. The first implementation called
`call_soon_threadsafe` per request from a tokio thread, and it was slower than
uvicorn with more loops than one. Refcounting is atomic on free-threaded
CPython, so every touch from a tokio thread was a contended atomic operation.

## Coalesced wakeups

A tokio thread pushes onto a lock-free queue and writes a single byte to a
socketpair — but only if no wakeup is already in flight. The worker's asyncio
loop wakes through `add_reader`, and one native drain callback schedules every
queued request.

A burst of a thousand requests therefore collapses into one wakeup instead of a
thousand. The flag is cleared *before* draining, never after, or a push racing
the drain is lost forever.

The same principle appears in topics and sockets: wake Python only when it is
actually idle. A busy stream never pays for a wakeup at all.

## Free-threaded CPython as the primary target

Not mainly for throughput. For topics.

Several worker loops in one process can share an in-memory topic, so a message
published on any loop reaches subscribers on all of them. Under a multiprocess
server — the usual way to get parallelism out of CPython — each worker holds a
private copy, and cross-worker fan-out fails silently: half the subscribers
never see the message and nothing reports an error.

The throughput argument is real too, and measured: a CPU-bound handler gains
3.15x on four loops on the free-threaded build, against 1.03x on the GIL build.

The GIL build still works, with a single worker loop. Requiring a specific
interpreter build to run at all would rule out most deployments.

## Async handlers only

Enforced at registration, so a synchronous handler is a `TypeError` at import
rather than a stall at runtime. A blocking call on a worker loop stalls every
request that loop is carrying, and accepting one silently would defer the
failure to production.

## Topics in Python, not Rust

The rest of the hot path is Rust, so this looks inconsistent. It is not.

The Rust argument is about *crossings*: keep the interpreter out of the network
path. Both ends of a topic are already Python, so moving the buffer into Rust
would add a foreign-function crossing on emit and another on receive, to
replace a `deque` operation cheaper than either crossing.

Choosing Rust because the rest of the project is Rust would have been the
mistake.

## One declaration, three interfaces

A handler already declares everything an OpenAPI operation and an MCP tool
need: a path, typed parameters, a body model, a return type and a docstring.
So all three — the router, the OpenAPI document and the agent capability — are
generated from one `RouteInfo`.

This is not a matter of tidiness. It is the only arrangement in which the
schema cannot describe an endpoint the server would refuse, and an agent cannot
be offered a tool that does not exist.

It follows that the route registry has to carry description and example fields
whether or not anything reads them yet. Adding them after two generators
already depend on the registry means changing both.

## Agent exposure is opt-in

Only routes marked `tool=True` reach `/mcp`. This default is load-bearing: the
alternative is that an administrative delete endpoint is agent-callable the
moment someone writes it.

## Fail safely, then let people opt out

Body limits, the request timeout, the connection cap, and returning no
exception detail to clients are all on by default, and every one of them costs
something. The rule is the same each time: a server that hangs, or leaks a
connection string in a `500` body, or grows by 800 MB from one upload, is worse
than a server that is 6% slower.

Each has an explicit escape hatch — `request_timeout=0`, `max_body=...`,
`debug=True` — for deployments where the trade-off has been measured.

## Approaches that were tried and replaced

Each of these shipped before it was replaced. They are documented because the
current behaviour only makes sense against the version it corrects.

**Bounding the queue is not backpressure.** The drain callback empties the queue
into asyncio tasks immediately, so a handler that awaits I/O leaves the queue
near empty while thousands of requests pile up inside the loop. The limit had
to count queued *plus in-flight*. The queue-only version would have passed a
CPU-bound test and protected nothing in production.

**A resource limit must never be released by garbage collection.** The
concurrency slot was freed by `Drop`, which looks deterministic and is not: a
finished SSE stream sits in a reference cycle, so its slot came back only when
the cyclic collector ran. Under light load the leak is invisible, which is what
makes it dangerous. The slot is now released by an explicit call.

**Rust must canonicalise whatever it validates.** Rust accepts ISO forms that
Python's own constructors reject, so input Rust had already judged valid could
raise on the Python side and become a `500`.

**Verify against a real client, not a reading of the spec.** The advertised MCP
protocol version was taken from a constant exported by the SDK, which is not the
same thing as a version a client will negotiate. Nothing short of a real client
attempting a handshake detects that, which is why the test suite drives the
official SDK client against a running server.
