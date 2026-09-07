#!/usr/bin/env python3
"""Values that must not break out of the wire format they are written into.

Two formats are line-oriented and therefore injectable: HTTP headers and the
`text/event-stream` body. In both, a value carrying a line break can end its own
field early and have whatever follows parsed as new structure.

Every case here was run against a running server before the fix. Three of them
were already safe and are kept as regression guards; three were live injections:

* `Event(id="1\\n\\ndata: ...")` ended the event and emitted a second one that
  the application never sent.
* `Event(event="tick\\ndata: ...")` injected a data line into the event.
* A string payload containing a carriage return was written as one `data:` line
  but read as two events, because the renderer split on `\\n` only while a
  client ends a line at `\\r` too.

The assertions read the raw socket rather than using an HTTP client: a client
parses the response and would hide the very thing under test.
"""
import socket
import sys
import threading
import time

from aether import App, Event, Request, Response, SSE
from aether.testing import free_port

failures: list[str] = []

#: A header value that, if written verbatim, ends the header and starts two
#: more, the first of which sets a cookie.
SPLIT = "ok\r\nSet-Cookie: pwned=1\r\nX-Injected: yes"

app = App(openapi_url=None, docs_url=None, mcp_url=None)


@app.get("/header-value")
async def header_value(_: Request):
    return Response(b"body", headers={"x-echo": SPLIT})


@app.get("/header-name")
async def header_name(_: Request):
    return Response(b"body", headers={"x-echo\r\nSet-Cookie: pwned=1": "v"})


@app.get("/via-middleware")
async def via_middleware(_: Request):
    return {"ok": True}


@app.middleware
async def add_header(request, call_next):
    reply = await call_next(request)
    if request.path == "/via-middleware":
        reply.headers["x-mw"] = SPLIT
    return reply


@app.get("/event-id")
async def event_id(_: Request):
    async def source():
        yield Event({"n": 1}, id='1\n\ndata: {"injected": true}\n\nid: 9')

    return SSE(source(), ping=None)


@app.get("/event-name")
async def event_name(_: Request):
    async def source():
        yield Event("hello", event="tick\ndata: injected-by-event-name")

    return SSE(source(), ping=None)


@app.get("/data-carriage-return")
async def data_carriage_return(_: Request):
    async def source():
        yield "line-one\r\rdata: injected-by-carriage-return"

    return SSE(source(), ping=None)


@app.get("/legitimate")
async def legitimate(_: Request):
    """Multi-line data and a well-formed id still have to work."""

    async def source():
        yield Event("first\nsecond", event="tick", id="42", retry=3000)

    return SSE(source(), ping=None)


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def fetch(port: int, path: str) -> str:
    """One request, one raw response, exactly as it went over the wire."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
    sock.settimeout(3.0)
    chunks = []
    try:
        while True:
            block = sock.recv(65536)
            if not block:
                break
            chunks.append(block)
    except socket.timeout:
        pass
    sock.close()
    return b"".join(chunks).decode("utf-8", "replace")


def headers_cannot_be_split(port: int) -> None:
    """A header carrying CRLF must never reach the wire as extra headers."""
    for path in ("/header-value", "/header-name", "/via-middleware"):
        wire = fetch(port, path)
        head = wire.split("\r\n\r\n", 1)[0]
        check(
            "set-cookie" not in head.lower(),
            f"{path} injected a Set-Cookie header into the response",
        )
        check(
            "x-injected" not in head.lower(),
            f"{path} injected an arbitrary header into the response",
        )
        # Refusing the response is the correct answer: the header is invalid
        # and the alternative is guessing what the handler meant.
        check(
            " 500 " in head.split("\r\n")[0] + " ",
            f"{path} returned {head.splitlines()[0]!r}, expected a 500",
        )


def sse_fields_cannot_be_split(port: int) -> None:
    """An event field carrying a line break must not emit extra structure."""
    body = fetch(port, "/event-id").split("\r\n\r\n", 1)[1]
    check("injected" not in body, "an event id injected a second event")
    check(
        body.count("data:") == 0,
        f"the stream should have ended without sending anything, got {body!r}",
    )

    body = fetch(port, "/event-name").split("\r\n\r\n", 1)[1]
    check("injected-by-event-name" not in body, "an event name injected a data line")


def sse_data_cannot_end_an_event(port: int) -> None:
    """A carriage return inside data is a line break to the client too.

    The renderer used to split payloads on `\n` alone, so a lone `\r` went out
    untouched inside a `data:` line and the client read the rest as a separate
    event. The check is exact: chunked framing only ever writes `\r\n`, so a
    carriage return that is not followed by a newline can only have come from
    the payload.
    """
    body = fetch(port, "/data-carriage-return").split("\r\n\r\n", 1)[1]
    stray = [i for i, ch in enumerate(body) if ch == "\r" and body[i + 1 : i + 2] != "\n"]
    check(
        not stray,
        f"a bare carriage return reached the client at {stray}, ending the event early",
    )
    check("line-one" in body, "the original payload was lost")
    check(
        "injected-by-carriage-return" in body,
        "the payload after the carriage return was dropped rather than escaped",
    )


def legitimate_events_still_work(port: int) -> None:
    body = fetch(port, "/legitimate").split("\r\n\r\n", 1)[1]
    for expected in ("event: tick", "id: 42", "retry: 3000", "data: first", "data: second"):
        check(expected in body, f"a valid event lost {expected!r}: {body!r}")


def main() -> None:
    port = free_port()
    server = app.build_server("127.0.0.1", port, workers=1)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    time.sleep(0.4)

    try:
        for step in (
            headers_cannot_be_split,
            sse_fields_cannot_be_split,
            sse_data_cannot_end_an_event,
            legitimate_events_still_work,
        ):
            try:
                step(port)
                print(f"  {step.__name__}: ok")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{step.__name__} raised {type(exc).__name__}: {exc}")
                print(f"  {step.__name__}: ERROR")
    finally:
        server.shutdown()
        thread.join(timeout=20)

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
