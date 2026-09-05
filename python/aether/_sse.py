"""Server-Sent Events.

Return an `SSE` from a handler and Aether streams it: headers go out
immediately, then every item the source yields becomes an event.

    @app.get("/feed")
    async def feed(_: Request):
        return SSE(app.topic("orders").subscribe())

The source is any async iterable, so a topic subscription is the common case
but an async generator works just as well.
"""

import json
from dataclasses import dataclass
from typing import Any

from ._schema import is_model_instance, to_json

#: `send_chunk` return codes, matching ChunkResult in src/responder.rs.
SENT = 0
FULL = 1
CLOSED = 2

SSE_HEADERS = [
    ("cache-control", "no-cache"),
    ("connection", "keep-alive"),
    # Tells nginx not to buffer the response, which would defeat the point.
    ("x-accel-buffering", "no"),
]


@dataclass(slots=True)
class Event:
    """One event, when the defaults are not enough.

    Yield plain values for the common case; yield this to set a name, an id for
    resumption, or a client retry hint.
    """

    data: Any
    event: str | None = None
    id: str | None = None
    retry: int | None = None


def _encode_data(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    if is_model_instance(data):
        return to_json(data).decode()
    return json.dumps(data, separators=(",", ":"))


def format_event(item: Any) -> bytes:
    """Render one item in the `text/event-stream` wire format."""
    if isinstance(item, Event):
        data, name, ident, retry = item.data, item.event, item.id, item.retry
    else:
        data, name, ident, retry = item, None, None, None

    lines: list[str] = []
    if name:
        lines.append(f"event: {name}")
    if ident is not None:
        lines.append(f"id: {ident}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    # A payload containing newlines has to become several data: lines, or the
    # blank line inside it would terminate the event early.
    for line in _encode_data(data).split("\n"):
        lines.append(f"data: {line}")
    return ("\n".join(lines) + "\n\n").encode()


class SSE:
    """A streaming `text/event-stream` response."""

    __slots__ = ("source", "ping", "status")

    def __init__(self, source: Any, *, ping: float | None = 15.0, status: int = 200) -> None:
        """`ping` sends a comment line when idle that long, which stops proxies
        and load balancers from closing an idle connection. None disables it."""
        if not hasattr(source, "__aiter__"):
            raise TypeError(
                f"SSE needs an async iterable, got {type(source).__name__}. "
                f"A topic subscription or an async generator both work"
            )
        self.source = source
        self.ping = ping
        self.status = status
