"""In-process pub/sub topics.

A topic is a named fan-out point. Producers emit, subscribers iterate:

    topic = app.topic("orders")

    await topic.emit({"id": 1})

    async for order in topic.subscribe():
        ...

Subscribers on *different* worker loops all receive every message. That works
because free-threaded CPython lets those loops share one process, which is the
whole reason D-002 targets 3.14t. Under a multiprocess server each worker would
hold its own private copy of every topic and a message emitted in one would
never reach the others.

**Why this is Python and not Rust.** The rest of the hot path is Rust because
tokio threads must never touch the interpreter. That argument does not apply
here: both ends of a topic are already Python, so moving the buffer into Rust
would add an FFI crossing on emit *and* on receive to replace a deque operation
that is cheaper than the crossing itself.

Waking a subscriber costs a `call_soon_threadsafe` only when it is actually
idle. A subscriber that is keeping up never blocks, so a busy stream coalesces
naturally, the same principle that made request dispatch fast.
"""

import asyncio
import threading
from collections import deque
from typing import Any

#: Drop the oldest buffered message. A slow subscriber loses history rather
#: than stalling every producer. The default.
DROP_OLDEST = "drop_oldest"
#: Drop the message being emitted.
DROP_NEWEST = "drop_newest"
#: Wait until the subscriber has room. Guarantees delivery, at the cost of one
#: slow subscriber holding up the producer.
BLOCK = "block"
#: Raise `TopicFull` at the producer.
ERROR = "error"

POLICIES = frozenset({DROP_OLDEST, DROP_NEWEST, BLOCK, ERROR})

DEFAULT_MAXSIZE = 1024


class TopicFull(Exception):
    """A subscriber's buffer is full and its policy is `error`."""


def _resolve(future: asyncio.Future) -> None:
    # Already-done covers the case where the waiter was cancelled, which happens
    # routinely when an SSE connection drops mid-await.
    if not future.done():
        future.set_result(None)


class Subscription:
    """One subscriber's view of a topic. Async-iterable, and closeable."""

    __slots__ = (
        "topic", "maxsize", "policy", "dropped",
        "_buffer", "_loop", "_lock", "_getter", "_putters", "_closed",
    )

    def __init__(self, topic: "Topic", maxsize: int, policy: str, loop) -> None:
        self.topic = topic
        self.maxsize = maxsize
        self.policy = policy
        self.dropped = 0
        self._buffer: deque[Any] = deque()
        self._loop = loop
        # Producers may run on other worker loops, so buffer and waiter state
        # are guarded rather than relying on any atomicity of deque itself.
        self._lock = threading.Lock()
        self._getter: asyncio.Future | None = None
        self._putters: deque[tuple[Any, asyncio.Future]] = deque()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def _try_offer(self, item: Any) -> bool:
        """Deliver without waiting.

        Returns False only when the policy is `block` and the buffer is full,
        which tells the caller to await `_offer` instead.
        """
        with self._lock:
            if self._closed:
                return True  # nothing to deliver to; not the producer's problem
            if len(self._buffer) >= self.maxsize:
                if self.policy == DROP_NEWEST:
                    self.dropped += 1
                    return True
                if self.policy == DROP_OLDEST:
                    self._buffer.popleft()
                    self.dropped += 1
                elif self.policy == ERROR:
                    raise TopicFull(
                        f"subscriber of topic {self.topic.name!r} is full "
                        f"({self.maxsize} buffered)"
                    )
                else:
                    return False
            self._buffer.append(item)
            getter, self._getter = self._getter, None

        if getter is not None:
            self._loop.call_soon_threadsafe(_resolve, getter)
        return True

    async def _offer(self, item: Any) -> None:
        """Deliver, waiting for room if the policy says to."""
        if self._try_offer(item):
            return
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if self._closed:
                    return
                if len(self._buffer) < self.maxsize:
                    self._buffer.append(item)
                    getter, self._getter = self._getter, None
                    break
                waiter = loop.create_future()
                self._putters.append((loop, waiter))
            await waiter

        if getter is not None:
            self._loop.call_soon_threadsafe(_resolve, getter)

    def close(self) -> None:
        """Stop the iterator and release anyone waiting on it."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            getter, self._getter = self._getter, None
            putters = list(self._putters)
            self._putters.clear()

        if getter is not None:
            self._loop.call_soon_threadsafe(_resolve, getter)
        for loop, waiter in putters:
            loop.call_soon_threadsafe(_resolve, waiter)
        self.topic._remove(self)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Any:
        while True:
            with self._lock:
                if self._buffer:
                    item = self._buffer.popleft()
                    waiting = self._putters.popleft() if self._putters else None
                    ready = True
                elif self._closed:
                    raise StopAsyncIteration
                else:
                    getter = self._loop.create_future()
                    self._getter = getter
                    ready = False

            if ready:
                if waiting is not None:
                    loop, waiter = waiting
                    loop.call_soon_threadsafe(_resolve, waiter)
                return item
            await getter

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.close()


class Topic:
    """A named fan-out point shared by every worker loop in the process."""

    __slots__ = ("name", "maxsize", "policy", "_subs", "_lock")

    def __init__(
        self, name: str, maxsize: int = DEFAULT_MAXSIZE, policy: str = DROP_OLDEST
    ) -> None:
        if policy not in POLICIES:
            raise ValueError(
                f"unknown policy {policy!r}; choose one of {', '.join(sorted(POLICIES))}"
            )
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self.name = name
        self.maxsize = maxsize
        self.policy = policy
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self, maxsize: int | None = None, policy: str | None = None) -> Subscription:
        """Start receiving. Must be called from inside a running event loop.

        The subscription binds to the loop it was created on, which is how a
        producer on another worker knows where to deliver.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "subscribe() needs a running event loop; call it inside a handler"
            ) from None

        policy = policy or self.policy
        if policy not in POLICIES:
            raise ValueError(
                f"unknown policy {policy!r}; choose one of {', '.join(sorted(POLICIES))}"
            )
        sub = Subscription(self, maxsize or self.maxsize, policy, loop)
        with self._lock:
            self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def _snapshot(self) -> list[Subscription]:
        with self._lock:
            return list(self._subs)

    async def emit(self, item: Any) -> int:
        """Deliver to every subscriber. Returns how many received it.

        Only awaits when a subscriber uses the `block` policy and is full.
        """
        delivered = 0
        for sub in self._snapshot():
            if not sub._try_offer(item):
                await sub._offer(item)
            delivered += 1
        return delivered

    def emit_nowait(self, item: Any) -> int:
        """Deliver without ever waiting.

        A `block` subscriber that is full is treated as `drop_newest`, because
        the alternative here would be blocking a thread that must not block.
        """
        delivered = 0
        for sub in self._snapshot():
            if not sub._try_offer(item):
                sub.dropped += 1
            delivered += 1
        return delivered

    def close(self) -> None:
        for sub in self._snapshot():
            sub.close()

    def __repr__(self) -> str:
        return f"<Topic {self.name!r} subscribers={self.subscribers}>"
