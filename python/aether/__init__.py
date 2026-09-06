"""Aether: a fast Python web framework with a Rust core.

Async handlers, a radix-tree router with typed path and query parameters
coerced in Rust, pydantic request and response bodies, bounded concurrency,
and OpenAPI 3.1 generated from the same route metadata.
"""

from ._app import App
from ._core import Request
from ._response import Response
from ._sessions import Session, Sessions
from ._sse import SSE, Event
from ._streams import BLOCK, DROP_NEWEST, DROP_OLDEST, ERROR, Subscription, Topic, TopicFull
from ._capabilities import Capability, CapabilityError
from ._depends import Depends
from ._logging import JsonFormatter, json_logging
from ._middleware import Reply
from ._redis import Consumer, Message, RedisBackend
from ._websocket import WebSocket, WebSocketClosed

__all__ = [
    "App", "Request", "Response", "SSE", "Event",
    "Topic", "Subscription", "TopicFull",
    "RedisBackend", "Consumer", "Message",
    "Capability", "CapabilityError", "Reply", "Depends", "json_logging", "JsonFormatter", "Sessions", "Session",
    "WebSocket", "WebSocketClosed",
    "BLOCK", "DROP_NEWEST", "DROP_OLDEST", "ERROR",
]
