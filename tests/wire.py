#!/usr/bin/env python3
"""HTTP the framework never writes itself, and input it did not choose.

Two areas the suites were silent on until 2026-09-08. Everything here was run
against a running server before anything was changed:

* **Protocol.** Chunked request bodies, `Expect: 100-continue`, pipelining,
  connection reuse, and the rule that 204 and 304 carry no body. All of it was
  already correct — hyper does the work — and none of it was asserted, so a
  change to the body or response path could have broken any of it silently.
* **Input.** Percent-encoding in a path was *not* decoded while the same
  framework decoded it in a query string, so a handler received `a%20b` from
  one and `a b` from the other. That was the one real defect.

Raw sockets throughout: an HTTP client normalises exactly the things under
test, and would have hidden the encoding bug entirely.
"""
import socket
import sys
import threading
import time

from aether import App, Request, Response
from aether.testing import free_port

failures: list[str] = []

MAX_BODY = 1024

app = App(openapi_url=None, docs_url=None, mcp_url=None)


@app.get("/hello")
async def hello(_: Request):
    return {"hello": "world"}


@app.post("/echo")
async def echo(req: Request):
    return {"len": len(req.body), "body": req.body.decode("utf-8", "replace")}


@app.get("/seg/{value}")
async def seg(_: Request, value: str):
    return {"value": value}


@app.get("/num/{value}")
async def num(_: Request, value: int):
    return {"value": value}


@app.get("/files/{*rest}")
async def files(_: Request, rest: str):
    return {"rest": rest}


@app.get("/q")
async def query(_: Request, v: str = "-"):
    return {"v": v}


@app.get("/nothing")
async def nothing(_: Request):
    return None


@app.get("/unchanged")
async def unchanged(_: Request):
    return Response(b"this body must not be sent", status=304, content_type="text/plain")


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


class Wire:
    """One connection, read one response at a time, so reuse can be tested."""

    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(3.0)
        self.buffer = b""

    def send(self, raw: bytes) -> None:
        self.sock.sendall(raw)

    def _fill(self) -> bool:
        try:
            block = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not block:
            return False
        self.buffer += block
        return True

    def response(self) -> tuple[str, dict[str, str], bytes]:
        """Read exactly one response, using its own framing to know where it ends."""
        while b"\r\n\r\n" not in self.buffer:
            if not self._fill():
                break
        head, _, rest = self.buffer.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").split("\r\n")
        status = lines[0] if lines else ""
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        length = int(headers.get("content-length", 0))
        chunked = headers.get("transfer-encoding", "") == "chunked"
        while chunked and not rest.endswith(b"0\r\n\r\n"):
            if not self._fill():
                break
            rest = self.buffer.partition(b"\r\n\r\n")[2]
        while not chunked and len(rest) < length:
            if not self._fill():
                break
            rest = self.buffer.partition(b"\r\n\r\n")[2]

        body = rest[:length] if not chunked else rest
        self.buffer = rest[length:] if not chunked else b""
        return status, headers, body

    def close(self) -> None:
        self.sock.close()


def once(port: int, raw: bytes) -> tuple[str, dict[str, str], bytes]:
    wire = Wire(port)
    try:
        wire.send(raw)
        return wire.response()
    finally:
        wire.close()


def get(port: int, target: bytes) -> tuple[str, dict[str, str], bytes]:
    return once(port, b"GET " + target + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")


# --------------------------------------------------------------------------
# I-030: protocol conformance
# --------------------------------------------------------------------------
def chunked_bodies(port: int) -> None:
    status, _, body = once(
        port,
        b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n",
    )
    check("200" in status, f"a chunked body returned {status!r}")
    check(b'"body":"hello world"' in body, f"a chunked body arrived as {body!r}")


def chunked_bodies_respect_the_limit(port: int) -> None:
    """The limit has to hold when the length is not declared up front.

    A `Content-Length` over the limit can be refused by reading the header. A
    chunked body cannot: the only defence is counting bytes as they arrive.
    """
    payload = b"".join(b"%x\r\n%s\r\n" % (200, b"z" * 200) for _ in range(10))
    status, _, _ = once(
        port,
        b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n" + payload + b"0\r\n\r\n",
    )
    check("413" in status, f"a chunked body over {MAX_BODY} bytes returned {status!r}")


def expect_continue(port: int) -> None:
    """A client that asks permission before sending must get an answer."""
    wire = Wire(port)
    try:
        wire.send(
            b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n"
            b"Expect: 100-continue\r\nConnection: close\r\n\r\nhello"
        )
        interim, _, _ = wire.response()
        check("100" in interim, f"Expect: 100-continue got {interim!r} instead of 100")
        final, _, body = wire.response()
        check("200" in final, f"the request after 100-continue returned {final!r}")
        check(b'"len":5' in body, f"the body after 100-continue was {body!r}")
    finally:
        wire.close()


def pipelining(port: int) -> None:
    """Two requests written together are answered in order, on one connection."""
    wire = Wire(port)
    try:
        wire.send(
            b"GET /hello HTTP/1.1\r\nHost: x\r\n\r\n"
            b"GET /seg/two HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        first_status, _, first_body = wire.response()
        second_status, _, second_body = wire.response()
        check("200" in first_status, f"first pipelined request returned {first_status!r}")
        check("200" in second_status, f"second pipelined request returned {second_status!r}")
        check(b'"hello"' in first_body, f"first response was {first_body!r}")
        check(b'"two"' in second_body, f"responses arrived out of order: {second_body!r}")
    finally:
        wire.close()


def connection_reuse(port: int) -> None:
    """A keep-alive connection serves more than one request."""
    wire = Wire(port)
    try:
        for i in range(3):
            wire.send(b"GET /hello HTTP/1.1\r\nHost: x\r\n\r\n")
            status, headers, body = wire.response()
            check("200" in status, f"request {i + 1} on a reused connection returned {status!r}")
            check(b'"hello"' in body, f"request {i + 1} returned {body!r}")
            check(
                headers.get("connection", "") != "close",
                f"request {i + 1} closed a keep-alive connection",
            )
    finally:
        wire.close()


def empty_responses_carry_no_body(port: int) -> None:
    """204 and 304 must not have one, whatever the handler returned."""
    status, headers, body = get(port, b"/nothing")
    check("204" in status, f"a handler returning None gave {status!r}")
    check(body == b"", f"a 204 carried a body: {body!r}")
    check("content-length" not in headers, "a 204 declared a Content-Length")

    status, _, body = get(port, b"/unchanged")
    check("304" in status, f"an explicit 304 gave {status!r}")
    check(body == b"", f"a 304 carried a body: {body!r}")


# --------------------------------------------------------------------------
# I-031: request input
# --------------------------------------------------------------------------
def path_parameters_are_decoded(port: int) -> None:
    """The defect this suite was written for.

    A path parameter arrived percent-encoded while a query parameter with the
    same content arrived decoded, so `/seg/a%20b` gave `a%20b` and `?v=a%20b`
    gave `a b`.
    """
    for target, expected, why in [
        (b"/seg/a%20b", '"a b"', "an encoded space"),
        (b"/seg/a%2Fb", '"a/b"', "an encoded slash"),
        (b"/seg/caf%C3%A9", '"caf\u00e9"', "encoded UTF-8"),
    ]:
        _, _, body = get(port, target)
        text = body.decode("utf-8", "replace")
        check(expected in text, f"{why} in a path gave {text!r}")


def decoding_happens_before_coercion(port: int) -> None:
    """`%34%32` is the digits of 42, so a typed parameter must see 42."""
    _, _, body = get(port, b"/num/%34%32")
    check(b'"value":42' in body, f"an encoded integer path parameter gave {body!r}")


def plus_is_a_space_only_in_a_query(port: int) -> None:
    _, _, body = get(port, b"/seg/a+b")
    check(b'"a+b"' in body, f"a plus in a path was changed: {body!r}")
    _, _, body = get(port, b"/q?v=a+b")
    check(b'"a b"' in body, f"a plus in a query was not a space: {body!r}")


def invalid_utf8_does_not_crash(port: int) -> None:
    """Undecodable bytes become replacement characters, not a 500."""
    for target in (b"/seg/%FF", b"/q?v=%FF"):
        status, _, body = get(port, target)
        check("200" in status, f"{target!r} returned {status!r}")
        check(b"\\ufffd" in body or b"\xef\xbf\xbd" in body, f"{target!r} gave {body!r}")


def catch_all_is_not_sanitised(port: int) -> None:
    """Documented behaviour, asserted so it cannot change silently.

    A catch-all hands over what the client sent, decoded. `..` is not stripped,
    in either form, because this is not a file server and a handler that builds
    a filesystem path from client input has to sanitise it either way.
    """
    for target in (b"/files/../secret", b"/files/%2E%2E/secret"):
        status, _, body = get(port, target)
        check("200" in status, f"{target!r} returned {status!r}")
        check(b'"../secret"' in body, f"{target!r} gave {body!r}")


def odd_query_strings(port: int) -> None:
    _, _, body = get(port, b"/q?v")
    check(b'"v":""' in body, f"a valueless query key gave {body!r}")
    _, _, body = get(port, b"/q?v=a%2Fb")
    check(b'"a/b"' in body, f"an encoded slash in a query gave {body!r}")
    status, _, _ = get(port, b"/seg/" + b"a" * 4000)
    check("200" in status, f"a 4000-character path segment returned {status!r}")


def main() -> None:
    port = free_port()
    server = app.build_server("127.0.0.1", port, workers=1, max_body=MAX_BODY)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    time.sleep(0.4)

    steps = [
        chunked_bodies,
        chunked_bodies_respect_the_limit,
        expect_continue,
        pipelining,
        connection_reuse,
        empty_responses_carry_no_body,
        path_parameters_are_decoded,
        decoding_happens_before_coercion,
        plus_is_a_space_only_in_a_query,
        invalid_utf8_does_not_crash,
        catch_all_is_not_sanitised,
        odd_query_strings,
    ]
    try:
        for step in steps:
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
