# Running a server

```python
app.run(host="0.0.0.0", port=8000)
```

`run` serves until interrupted. Every option below can also be passed to
`app.build_server(...)`, which prepares a server without starting it — that is
what the [test client](guide/testing.md) uses.

| option | default | what it does |
|---|---|---|
| `host` | `127.0.0.1` | interface to bind |
| `port` | `8000` | port to bind |
| `workers` | detected | Python worker loops |
| `max_concurrency` | 1024 | requests per worker, queued plus in-flight |
| `max_connections` | 2048 | sockets held open |
| `max_body` | 16 MiB | largest request body |
| `request_timeout` | 30.0 | seconds to a handler's first response |
| `shutdown_grace` | 10.0 | seconds Ctrl-C waits for in-flight requests |

## Worker loops

Each worker is one OS thread running one asyncio loop. They share the process,
which is what lets a topic reach subscribers on all of them.

The default is measured, not guessed:

- **On a GIL build, always one.** One loop was best or tied at every handler
  cost in the sweep; extra loops only cost throughput.
- **On a free-threaded build, one is never best.** Even a handler that does
  nothing gains from more loops, and a handler doing 500µs of work gains 3.4x.
  Throughput peaks around the performance-core count and falls off once the
  efficiency cores are oversubscribed.

So the default is "how many cores can actually run Python in parallel", which
is not `os.cpu_count()`: that counts efficiency cores, and inside a container it
reports the host's cores rather than the cgroup quota — which would start
dozens of loops for a two-CPU limit. Aether probes cgroup quotas, CPU affinity
and performance-core counts, takes the most constrained answer, and caps it at
8.

The cap is a guard against an absurd probe result, not a measured ceiling.
Raise it explicitly on a large homogeneous machine with CPU-heavy handlers:

```python
app.run(workers=16)
```

!!! note "More loops is for CPU, not for I/O"

    Awaiting I/O does not need more loops. An `await` yields the loop, so one
    loop holds thousands of them. What needs more loops is CPU time spent
    inside the handler.

## Backpressure

Each worker accepts at most `max_concurrency` requests at once, counting both
those queued and those already running. Past that the request tries another
worker, and if every worker is full the server answers `503` with
`Retry-After: 1`.

```python
app.run(max_concurrency=1024)   # the default, per worker loop
```

**Counting in-flight requests is the part that matters.** The drain callback
empties the queue into asyncio tasks immediately, so a handler that awaits I/O
leaves the queue near empty while thousands of requests pile up inside the
loop. Bounding the queue alone would look like backpressure and protect
nothing.

Lower it for slow handlers, where a deep backlog only adds latency before an
inevitable client timeout. Raise it to absorb larger bursts of fast requests.

`max_connections` is a separate limit, because an idle keep-alive connection
costs a file descriptor without ever reaching a worker. At that limit the
server stops accepting rather than refusing, so the wait lands in the OS
backlog where a client's own connect timeout governs it.

## In front of it

Aether speaks HTTP/1.1 with no TLS and no HTTP/2. **Put a terminating proxy in
front of it** — nginx, Caddy, a cloud load balancer — and let that handle TLS,
HTTP/2 and whatever else the edge needs.

## Containers

The repository ships a multi-stage `Dockerfile` that installs free-threaded
3.14 with `uv`, builds the wheel, and installs it into a slim runtime image.
The official Python images carry no free-threaded interpreter, so installing it
explicitly keeps the container and the development machine on the same
interpreter.

```bash
make image     # build aether:dev
make stack     # two nodes against one redis
make down      # stop everything
```

Bind to `0.0.0.0` inside a container. Anything listening only on loopback is
unreachable from outside it.

!!! warning "Do not benchmark through Docker"

    Docker Desktop on macOS measured **3.4x slower** than native for the same
    image, and the cost is the VM's port boundary rather than anything in the
    code. Container numbers and native numbers are not comparable. See
    [performance](design/performance.md).

## Shutdown

Ctrl-C stops accepting, waits up to `shutdown_grace` for in-flight requests,
then stops regardless. Streams and sockets are closed.
