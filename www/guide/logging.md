# Logging

Everything Aether reports goes through the standard library `logging` module,
under the `aether` logger. Nothing is written to stderr directly, so it lands
wherever your application already sends its logs.

```python
import logging

logging.basicConfig(level=logging.INFO)
```

## JSON lines

```python
from aether import json_logging

json_logging()          # one JSON object per line, on stderr
```

It attaches a handler to the `aether` logger and stops it propagating to the
root, so Aether's own output becomes JSON without changing how the rest of
your application logs.

Each record carries the time, level, logger name and message, plus any extra
fields attached to the record. Exceptions are included as formatted text.

## Access log

Off by default: it costs something per request, and many deployments already
log at the proxy.

```python
app = App(access_log=True)
```

One line per request with the method, path, status and duration, on the
`aether.access` logger, with the same fields attached as record extras so the
JSON formatter emits them as fields. It is registered as the first middleware,
so it wraps everything and sees the final status, including one set by other
middleware.

The access line records the status only. A handler that raised is logged once,
with its traceback, by the runtime — not twice, once by each.

## Quieting it

```python
logging.getLogger("aether").setLevel(logging.WARNING)         # everything
logging.getLogger("aether.access").setLevel(logging.WARNING)  # just the access log
```

## What Aether logs on its own

- A handler exception, with its traceback, at `ERROR`
- An exception raised by a WebSocket handler, or by a dependency's teardown
- A dropped Redis connection behind a durable topic, once per outage at
  `WARNING`, and once more when it reconnects

The one line Aether still prints directly is the banner `app.run` writes at
startup, before any logging configuration can be assumed to exist.

Handler tracebacks go to the log and never to the client. See
[Errors and limits](errors.md).
