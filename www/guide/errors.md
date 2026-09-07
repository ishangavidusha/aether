# Errors and limits

## A handler that raises

The client gets `500` with no detail. The traceback goes to the log.

Exception messages routinely carry connection strings, file paths, query
fragments and user data. Returning them to whoever triggered the exception is
how that information leaks.

During development:

```python
app = App(debug=True)   # include the exception text in the 500 body
```

`debug` is per server, never module state. Two apps in one process do not share
it.

## Validation

A parameter or body that fails validation is a `422` in one shape, whatever
failed:

```json
{"detail": [{"type": "...", "loc": ["query", "limit"], "msg": "..."}]}
```

Path and query failures are produced in Rust, before a worker is woken. Body
failures come from pydantic, on the worker.

## Limits

| limit | default | what happens past it |
|---|---|---|
| `max_body` | 16 MiB | `413`, without buffering the body |
| `max_concurrency` | 1024 per worker | `503` with `Retry-After: 1` |
| `max_connections` | 2048 | the listener stops accepting; the OS backlog holds the wait |
| `request_timeout` | 30s | `504`, and the connection is freed |
| header read timeout | 15s | the connection is dropped |
| `shutdown_grace` | 10s | in-flight requests are abandoned |

```python
app.run(
    max_body=64 * 1024 * 1024,
    max_concurrency=256,
    max_connections=4096,
    request_timeout=0,        # disabled
)
```

## Timeouts and streams

`request_timeout` measures the wait for a handler's *first* response. It does
not cut short a stream that has already started, so SSE and WebSocket
connections are unaffected by it and can stay open for as long as they like.

It costs about 5-8% of hello-world throughput. Set it to `0` for a service
whose handlers are legitimately long-running.

## Shutdown

Ctrl-C stops accepting new connections and waits up to `shutdown_grace` for
in-flight requests to finish before stopping anyway. Open streams and sockets
are closed.
