"""Code that runs *inside* the Python worker threads.

Rust calls `make_worker_loop` once per worker thread and registers a native
drain callback with `loop.add_reader`. When requests are queued, that callback
runs on this thread and schedules `run_handler` for each one, with path
parameters already coerced to Python objects.
"""

import asyncio
import datetime
import uuid

from ._logging import logger
from ._middleware import Reply, merge
from ._response import Response
from ._schema import RequestValidationError, is_model_instance, to_json
from ._sse import CLOSED, SSE, SSE_HEADERS, format_event
from ._websocket import WebSocket


async def run_websocket(handler, request, responder, core, params):
    """Run one WebSocket handler.

    `responder` is never used to send a reply here; the 101 already went out
    from the accept path. It is held only so the worker's in-flight count is
    released when this task ends, exactly as it is for an ordinary request.
    """
    loop = asyncio.get_running_loop()
    socket = WebSocket(core)
    gone = loop.create_future()

    def _peer_left():
        if not gone.done():
            gone.set_result(None)

    core.on_close(loop, _peer_left)

    if params is None:
        task = asyncio.ensure_future(handler(request, socket))
    else:
        task = asyncio.ensure_future(handler(request, socket, **params))

    try:
        # Racing the handler against the close is what lets the common pattern
        # work: a handler blocked on `async for item in topic.subscribe()` has
        # no reason to notice its peer left, and would otherwise hold that
        # subscription and its in-flight slot forever.
        done, _ = await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
        if task not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            exc = task.exception()
            if exc is not None:
                logger.exception("websocket handler raised", exc_info=exc)
    finally:
        core.close()
        responder.finish()


async def pump_sse(sse, responder):
    """Stream one SSE response until the source ends or the client leaves.

    This holds its worker's in-flight slot for the life of the connection,
    which is correct: a live stream is a request still being served, and it
    should count against `max_concurrency` like any other.

    The disconnect future matters more than it looks. Without it, a stream
    waiting on a quiet topic would sit in `__anext__` indefinitely and never
    discover its client had gone, leaking the subscription and the slot until
    something happened to be published.
    """
    loop = asyncio.get_running_loop()
    gone = loop.create_future()

    def _client_left():
        if not gone.done():
            gone.set_result(None)

    responder.start_stream(sse.status, "text/event-stream; charset=utf-8", SSE_HEADERS)
    responder.notify_disconnect(loop, _client_left)

    iterator = sse.source.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())

            done, _ = await asyncio.wait(
                {pending, gone}, timeout=sse.ping, return_when=asyncio.FIRST_COMPLETED
            )

            if gone in done:
                break

            if pending in done:
                try:
                    item = pending.result()
                except StopAsyncIteration:
                    pending = None
                    break
                pending = None
                if responder.send_chunk(format_event(item)) == CLOSED:
                    break
            elif sse.ping is not None:
                # Idle. A comment line keeps proxies from closing the stream,
                # and doubles as a liveness check.
                if responder.send_chunk(b": ping\n\n") == CLOSED:
                    break
    finally:
        if pending is not None:
            pending.cancel()
        responder.end_stream()
        # Releases the topic subscription, if that is what was being iterated.
        close = getattr(sse.source, "close", None)
        if close is not None:
            close()


#: Constructors the Rust side calls for parameter types it validated but
#: cannot build. Values reaching these have already been canonicalised in Rust,
#: so they cannot fail here.
make_uuid = uuid.UUID
make_date = datetime.date.fromisoformat
make_datetime = datetime.datetime.fromisoformat


def make_worker_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


async def run_handler(handler, request, responder, params, debug):
    """Await one handler and turn whatever it returns into a response.

    `params` is None for routes with no path parameters, which keeps the
    common case free of an extra dict and an unpacking call.

    `debug` is passed per server rather than held as module state, so two apps
    in one process cannot end up sharing one another's setting.
    """
    try:
        await _respond(handler, request, responder, params, debug)
    finally:
        # Frees the worker's concurrency slot now rather than whenever Python
        # happens to collect the responder.
        responder.finish()


async def _respond(handler, request, responder, params, debug):
    try:
        result = await (handler(request) if params is None else handler(request, **params))
    except RequestValidationError as exc:
        responder.send(422, "application/json", exc.body)
        return
    except Exception as exc:  # noqa: BLE001 - a handler crash must still answer
        # The detail goes to the server's log. The client gets a status and
        # nothing else, unless the app was started with debug=True.
        logger.exception(
            "handler raised",
            exc_info=exc,
            extra={"method": request.method, "path": request.path},
        )
        detail = (
            f"{type(exc).__name__}: {exc}".encode() if debug else b"internal server error"
        )
        responder.send(500, "text/plain; charset=utf-8", detail)
        return

    status_override = None
    extra_headers = None
    if isinstance(result, Reply):
        result, status_override, extra_headers = merge(result)

    if isinstance(result, SSE):
        await pump_sse(result, responder)
    elif isinstance(result, Response):
        responder.send(
            result.status, result.content_type, result.encoded(), result.header_list()
        )
    elif result is None:
        responder.send(status_override or 204, "text/plain", b"", extra_headers)
    elif is_model_instance(result):
        # pydantic serializes straight to bytes, so this skips both a Python
        # str and our own JSON encoder.
        responder.send(
            status_override or 200, "application/json", to_json(result), extra_headers
        )
    elif isinstance(result, (bytes, bytearray, memoryview)):
        responder.send(
            status_override or 200,
            "application/octet-stream",
            bytes(result),
            extra_headers,
        )
    elif isinstance(result, str):
        responder.send(
            status_override or 200,
            "text/plain; charset=utf-8",
            result.encode(),
            extra_headers,
        )
    elif status_override is not None or extra_headers is not None:
        # send_json cannot carry a status or headers, so encode here instead.
        import json as _json

        responder.send(
            status_override or 200,
            "application/json",
            _json.dumps(result, default=str).encode(),
            extra_headers,
        )
    else:
        responder.send_json(200, result)
