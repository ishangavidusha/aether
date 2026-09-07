# Performance

## Method

`bench/run.py` starts each target as a subprocess, waits for the port, warms it
up for 2 seconds, then measures with [`oha`](https://github.com/hatoo/oha) at 64
connections for 8 seconds. `raw` targets are a bare ASGI callable returning a
pre-encoded body, which is the best case for the comparison servers. `Nw` means
one OS process per CPU.

**Machine.** Apple Silicon, macOS 25.6, 10 cores (4 performance + 6
efficiency). Python 3.14.7, both builds, installed by uv. Rust 1.92, release
profile with fat LTO. uvicorn 0.52.4, granian 2.8.2, FastAPI 0.141.1.

```bash
make bench        # hello world, free-threaded
make bench-gil    # hello world, GIL build
make bench-cpu    # CPU-bound handler scaling
make sweep        # handler cost against loop count
```

!!! warning "Read this before quoting any number here"

    **Hello world measures dispatch, not a framework.** It answers whether a
    dispatch design is worth building. It answers nothing about a real
    application, where a single database call dwarfs everything measured below.

    **Record the load average.** A machine still busy from a previous run
    reports regressions that do not exist; one such 3.5% drop traced entirely
    to leftover benchmark load. The runners capture `os.getloadavg()` with
    every result and warn above 2.0. Absolute numbers compare across sessions
    only at similar starting load; ratios inside one run are always sound,
    since every target faces the same machine.

    **Never benchmark through Docker.** Docker Desktop on macOS measured 3.4x
    slower for the same image, and the cost is the VM's port boundary, not the
    code.

## Hello world

Free-threaded 3.14.7, 64 connections:

| target | req/s | p50 ms | p99 ms |
|---|---:|---:|---:|
| Aether, 4 loops | 181,397 | 0.28 | 1.46 |
| granian raw ASGI | 135,846 | 0.46 | 0.72 |
| granian + FastAPI | 29,674 | 2.14 | 2.51 |
| uvicorn raw ASGI, 10 workers | 66,474 | 0.65 | 4.61 |
| uvicorn raw ASGI | 19,493 | 3.30 | 3.42 |
| uvicorn + FastAPI | 12,411 | 5.17 | 5.36 |

Granian is the closest comparison: same shape, a Rust server driving a Python
event loop, and therefore the number to watch when this design changes. The
FastAPI rows answer a different question, since they include a framework doing
framework work.

## What the design was worth

Dispatch was rewritten from `call_soon_threadsafe` per request to a lock-free
queue with coalesced wakeups.

| build / loops | before | after | change |
|---|---:|---:|---:|
| free-threaded, 1 loop | 35,158 | 189,015 | **5.4x** |
| free-threaded, 10 loops | 14,114 | 171,304 | **12.1x** |
| GIL, 1 loop | 63,163 | 192,054 | **3.0x** |

The original numbers showed three things at once, and each of them was a
design error: throughput *fell* as loops were added, because every request woke
an idle loop and no wakeup coalesced; the free-threaded build was slower than
the GIL build, because refcounting is atomic on 3.14t and tokio threads were
touching Python objects; and the whole thing sat far below granian, which meant
the gap was dispatch rather than HTTP.

## Free-threading actually delivers

A CPU-bound handler — a 20,000-iteration Python loop, about 376µs — at 32
connections:

| loops | free-threaded | speedup | GIL | speedup |
|---:|---:|---:|---:|---:|
| 1 | 2,659 | 1.00x | 2,308 | 1.00x |
| 2 | 5,205 | 1.96x | 2,372 | 1.03x |
| 4 | 8,380 | **3.15x** | 2,375 | 1.03x |
| 8 | 7,201 | 2.71x | 2,357 | 1.02x |

The GIL build is flat, exactly as predicted. The ceiling on the free-threaded
build is the performance-core count, not the core count: 8 loops on a 4+6
machine oversubscribes the efficiency cores and loses ground.

## How many loops

Handler CPU cost against loop count, free-threaded:

| handler µs | 1 loop | 2 loops | 4 loops | 8 loops | gain over 1 |
|---:|---:|---:|---:|---:|---:|
| 0 | 184,539 | 190,349 | 193,172 | 173,880 | 1.05x |
| 10 | 95,933 | 116,977 | 114,443 | 137,985 | 1.44x |
| 50 | 19,159 | 43,052 | 53,605 | 47,497 | 2.80x |
| 100 | 10,127 | 20,946 | 30,461 | 27,205 | 3.01x |
| 500 | 2,154 | 4,202 | 7,347 | 6,031 | 3.41x |

**There is no crossover.** Extra loops win at every handler cost, including
zero, which is why one loop is never the default on a free-threaded build. The
same sweep on the GIL build is flat and extra loops only cost throughput, which
is why one loop is always the default there.

The sweep also contradicts an intuitive prediction: that dispatch should prefer
fewer loops, because wakeup coalescing is diluted when arrivals spread across
many of them. The measurements do not support it. The only significant effect is
that oversubscribing the performance cores hurts.

## What features cost

| change | cost |
|---|---|
| typed path parameter | nothing measurable |
| query parameters | nothing measurable on routes that declare none |
| pydantic body validation | ~13% against hello world |
| `request_timeout=30` | 5-8% |
| headers on every request, explicit slot release | the rest of ~12% at M6 |

The request timeout is on by default despite that cost. A handler that hangs
otherwise holds a connection and a concurrency slot indefinitely. The same
trade-off governs body limits and error detail: fail safely by default, and set
`request_timeout=0` once you have measured your own workload.

Numbers taken before that change describe a different server and are not
comparable with the ones above.

## What has not been measured

These are open, not assumed. An unmeasured claim is not a result:

- **A handler that awaits** rather than burns CPU — a 1-5ms database call. The
  sweep covers CPU cost only, and an `await` yields the loop, so the two should
  behave very differently.
- **Latency under sustained overload**, now that backpressure exists.
- **Memory per worker loop.**
- **Scaling past 8 loops** on a large homogeneous Linux machine. The cap of 8
  is a guard against an absurd probe result, not a measured ceiling; this
  machine has four performance cores and cannot answer the question.
- **Streaming throughput.** SSE and WebSocket have correctness tests and no
  performance numbers at all.
