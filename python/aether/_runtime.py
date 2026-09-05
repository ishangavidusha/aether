"""Code that runs *inside* the Python worker threads.

Rust calls `make_worker_loop` once per worker thread and registers a native
drain callback with `loop.add_reader`. When requests are queued, that callback
runs on this thread and schedules `run_handler` for each one, with path
parameters already coerced to Python objects.
"""

import asyncio


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
    except Exception as exc:  # noqa: BLE001 - spike: surface anything
        responder.send(500, "text/plain; charset=utf-8", f"{type(exc).__name__}: {exc}".encode())
        return

    if result is None:
        responder.send(204, "text/plain", b"")
    elif isinstance(result, (bytes, bytearray, memoryview)):
        responder.send(200, "application/octet-stream", bytes(result))
    elif isinstance(result, str):
        responder.send(200, "text/plain; charset=utf-8", result.encode())
    else:
        responder.send_json(200, result)
