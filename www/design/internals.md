# Internals

How a request actually moves through the process. Useful if you are reading the
source, debugging something odd, or deciding whether this design suits your
workload.

## Layout

```
src/            Rust crate, built as the aether._core extension module
  server.rs     tokio accept loop, hyper 1, HEAD/405/413, upgrade handshake
  router.rs     matchit radix tree per method, path and query coercion
  queue.rs      bounded per-worker queue + socketpair wakeup
  worker.rs     one OS thread + one asyncio loop per worker, drain callback
  request.rs    the frozen Request handed to handlers
  responder.rs  reply channel, streaming bodies, client-disconnect signal
  websocket.rs  tokio-tungstenite bridge
python/aether/  App, routing, pydantic, OpenAPI, topics, SSE, sockets, runtime
```

## The request path

1. A tokio thread accepts the connection and hyper parses the request.
2. The router matches it against a radix tree built per HTTP method, and
   coerces path and query parameters into owned Rust values. A request that
   cannot succeed — bad path parameter, missing required query parameter, wrong
   method, body over the limit — is answered here, and no Python worker is ever
   woken.
3. The request becomes a plain Rust struct and is pushed onto a worker's
   bounded queue. If the queue plus in-flight count is at the limit it tries
   another worker; if every worker is full the answer is `503`.
4. If no wakeup is already in flight, one byte goes down a socketpair.
5. The worker's asyncio loop wakes through `add_reader`. A native drain
   callback clears the flag, pops **every** queued request, and schedules each
   handler in that one callback.
6. The handler returns. A dict is serialized to JSON in Rust; a pydantic model
   through pydantic's own serializer. The bytes travel back to the waiting
   tokio task over a oneshot channel.

Two properties carry the design: Python is only ever touched from the worker's
own thread, and a burst of requests collapses into one wakeup.

## Invariants

These are load-bearing. Breaking one silently destroys performance or
deadlocks.

**Tokio threads never attach to the interpreter.** No `Python::attach`, no
`Py::new`, no refcount touch. Worth 5.4x.

**Never block in native code while attached.** Any blocking wait is wrapped in
`py.detach`. Blocking while attached deadlocks free-threaded CPython at a
stop-the-world point — which is exactly how this framework's first startup
deadlock happened.

**Wakeups coalesce.** At most one wake byte in flight, and the flag is cleared
*before* draining. Clearing it after loses a racing push.

**Handlers are `async def`**, enforced at registration.

**Both Python builds work.** Free-threaded 3.14t is the primary target; the GIL
build runs a single worker loop.

**Agent exposure is opt-in.** Only `tool=True` routes reach `/mcp`.

**A resource limit is never released by garbage collection.** The concurrency
slot is freed by an explicit call, never by `Drop` and never by the cyclic
collector.

**Anything Rust validates, Rust canonicalises**, so Python's constructors
cannot fail on input Rust already accepted.

**Never return exception detail to a client.** Tracebacks go to the log;
`debug=True` is the only exception, and that flag is per server, never module
state.

## Concurrency model

One OS thread per worker, each running one asyncio loop, all in one process.
Handlers on different loops run Python in parallel on a free-threaded build.

The `Request` is frozen, so it needs no locking. Topics are shared across loops
and each subscription is bound to the loop that created it, which is how a
producer on another worker knows where to deliver.

## Streaming

A streaming response holds a guard that hyper drops when the connection ends.
The pump races the next message against that guard, so a disconnected client is
noticed without polling — including on a stream that is sitting idle on a quiet
topic, which has nothing to write and therefore nothing that would fail.

## Testing

Fifteen standalone scripts under `tests/`, each exiting non-zero on failure,
run against a real server on a real socket.

```bash
make verify        # free-threaded
make verify-gil    # GIL build
```

`tests/verify.py` drives 5,000 concurrent requests, each carrying a unique
token, and asserts every response comes back with its own. That is the check
that matters for queue dispatch, where the plausible bug is a reply delivered to
the wrong request.

Each case in `tests/hardening.py` names a defect that was demonstrated against
a running server before it was fixed.

## Known gaps

- No TLS and no HTTP/2. Expects a terminating proxy in front.
- Middleware does not wrap socket handlers, only their authorizer.
- MCP is POST/JSON only: no streaming responses, no server-to-client channel,
  no resource subscriptions.
- Worker assignment is round-robin. Whether one loop's queue backs up while
  others idle, under slow handlers, has not been measured.
