"""Code that runs *inside* the Python worker threads.

Rust calls `make_worker_loop` once per worker thread and registers a native
drain callback with `loop.add_reader`. When requests are queued, that callback
runs on this thread and schedules `run_handler` for each one, with path
parameters already coerced to Python objects.
"""

import asyncio

from ._response import Response
from ._schema import RequestValidationError, is_model_instance, to_json
from ._sse import CLOSED, SSE, SSE_HEADERS, format_event


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


def make_worker_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


async def run_handler(handler, request, responder, params):
    """Await one handler and turn whatever it returns into a response.

    `params` is None for routes with no path parameters, which keeps the
    common case free of an extra dict and an unpacking call.
    """
    try:
        result = await (handler(request) if params is None else handler(request, **params))
    except RequestValidationError as exc:
        responder.send(422, "application/json", exc.body)
        return
    except Exception as exc:  # noqa: BLE001 - spike: surface anything
        responder.send(500, "text/plain; charset=utf-8", f"{type(exc).__name__}: {exc}".encode())
        return

    if isinstance(result, SSE):
        await pump_sse(result, responder)
    elif isinstance(result, Response):
        responder.send(
            result.status, result.content_type, result.encoded(), result.header_list()
        )
    elif result is None:
        responder.send(204, "text/plain", b"")
    elif is_model_instance(result):
        # pydantic serializes straight to bytes, so this skips both a Python
        # str and our own JSON encoder.
        responder.send(200, "application/json", to_json(result))
    elif isinstance(result, (bytes, bytearray, memoryview)):
        responder.send(200, "application/octet-stream", bytes(result))
    elif isinstance(result, str):
        responder.send(200, "text/plain; charset=utf-8", result.encode())
    else:
        responder.send_json(200, result)
