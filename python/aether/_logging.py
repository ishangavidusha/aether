"""Logging.

Everything Aether reports goes through the standard `logging` module, under the
`aether` logger, so it lands wherever an application already sends its logs.
Nothing is printed to stderr directly any more; a framework that writes past
your logging setup is a framework you cannot run in production.

    import logging
    logging.basicConfig(level=logging.INFO)

For machine-readable output:

    from aether import json_logging
    json_logging()

`access_log` on the app adds one line per request. It is opt-in because it is a
per-request cost and because many deployments already log at the proxy.
"""

import json
import logging
import time
from typing import Any

logger = logging.getLogger("aether")
access_logger = logging.getLogger("aether.access")

#: Attributes `logging` puts on every record. Anything else was added by the
#: caller and belongs in the JSON output.
_STANDARD = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info taskName thread threadName""".split()
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any extra fields included."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def json_logging(level: int = logging.INFO) -> None:
    """Send Aether's logs to stderr as JSON lines."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("aether")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def implied_status(value: Any) -> int:
    """The status a handler's return value would produce on its own."""
    from ._response import Response

    if isinstance(value, Response):
        return value.status
    return 204 if value is None else 200


async def access_middleware(request, call_next):
    """One log line per request. Registered when `access_log=True`."""
    started = time.perf_counter()
    try:
        reply = await call_next(request)
    except Exception:
        # The access line only records that it failed. The traceback is logged
        # once, by the runtime, rather than at every layer it passes through.
        access_logger.warning(
            "%s %s 500", request.method, request.path,
            extra={
                "method": request.method,
                "path": request.path,
                "status": 500,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        raise

    status = reply.status if reply.status is not None else implied_status(reply.value)
    access_logger.info(
        "%s %s %s",
        request.method,
        request.path,
        status,
        extra={
            "method": request.method,
            "path": request.path,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return reply
