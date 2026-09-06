#!/usr/bin/env python3
"""Router behaviour: path parameters, coercion, and the error responses.

Registration-time checks run in-process. HTTP behaviour runs against a live
server, because coercion happens in Rust before a worker is woken and that path
cannot be exercised from Python alone.
"""
import sys
import threading
import uuid

import httpx

from aether import App, Request

PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"

app = App()


@app.get("/users/{user_id}")
async def get_user(_: Request, user_id: int):
    return {"user_id": user_id, "type": type(user_id).__name__}


@app.post("/users/{user_id}")
async def update_user(_: Request, user_id: int):
    return {"updated": user_id}


@app.get("/things/{name}")
async def get_thing(_: Request, name: str):
    return {"name": name, "type": type(name).__name__}


@app.get("/scores/{value}")
async def get_score(_: Request, value: float):
    return {"value": value, "type": type(value).__name__}


@app.get("/flags/{on}")
async def get_flag(_: Request, on: bool):
    return {"on": on, "type": type(on).__name__}


@app.get("/orgs/{org}/repos/{repo}/issues/{number}")
async def get_issue(_: Request, org: str, repo: str, number: int):
    return {"org": org, "repo": repo, "number": number}


@app.get("/files/{*rest}")
async def get_file(_: Request, rest: str):
    return {"rest": rest}


@app.get("/plain")
async def plain(_: Request):
    return {"ok": True}


CASES = [
    # (method, path, expected status, expected json subset or None)
    ("GET", "/users/42", 200, {"user_id": 42, "type": "int"}),
    ("GET", "/users/-7", 200, {"user_id": -7, "type": "int"}),
    ("GET", "/users/abc", 422, None),
    ("GET", "/users/4.5", 422, None),
    ("GET", "/users/", 404, None),
    ("GET", "/things/hello", 200, {"name": "hello", "type": "str"}),
    ("GET", "/things/a%20b", 200, {"name": "a%20b", "type": "str"}),
    ("GET", "/scores/1.5", 200, {"value": 1.5, "type": "float"}),
    ("GET", "/scores/2", 200, {"value": 2.0, "type": "float"}),
    ("GET", "/scores/nan", 422, None),
    ("GET", "/flags/true", 200, {"on": True, "type": "bool"}),
    ("GET", "/flags/0", 200, {"on": False, "type": "bool"}),
    ("GET", "/flags/maybe", 422, None),
    ("GET", "/orgs/acme/repos/api/issues/9", 200, {"org": "acme", "repo": "api", "number": 9}),
    ("GET", "/files/a/b/c.txt", 200, {"rest": "a/b/c.txt"}),
    ("GET", "/plain", 200, {"ok": True}),
    ("GET", "/nope", 404, None),
    ("DELETE", "/users/42", 405, None),
    ("PUT", "/plain", 405, None),
]


def registration_checks() -> list[str]:
    """Every one of these should fail loudly at decoration time."""
    failures = []

    def expect_error(label, exc_type, fn):
        try:
            fn()
        except exc_type:
            return
        except Exception as e:
            failures.append(f"{label}: raised {type(e).__name__} not {exc_type.__name__}: {e}")
            return
        failures.append(f"{label}: no error raised")

    def missing_param():
        bad = App()

        @bad.get("/a/{x}")
        async def h(_: Request):
            return {}

    def unannotated_extra():
        bad = App()

        @bad.get("/a")
        async def h(_: Request, x):
            return {}

    def unsupported_type():
        bad = App()

        @bad.get("/a/{x}")
        async def h(_: Request, x: dict):
            return {}

    def sync_handler():
        bad = App()

        @bad.get("/a")
        def h(_: Request):
            return {}

    def no_request_arg():
        bad = App()

        @bad.get("/a")
        async def h():
            return {}

    def typed_wildcard():
        bad = App()

        @bad.get("/a/{*rest}")
        async def h(_: Request, rest: int):
            return {}

    expect_error("path param missing from handler", TypeError, missing_param)
    expect_error("unannotated extra argument", TypeError, unannotated_extra)
    expect_error("unsupported param type", TypeError, unsupported_type)
    expect_error("sync handler", TypeError, sync_handler)
    expect_error("handler with no request arg", TypeError, no_request_arg)
    expect_error("typed wildcard", TypeError, typed_wildcard)

    # UUID became a supported path type in M6; it used to be an error here.
    try:
        ok = App()

        @ok.get("/a/{x}")
        async def uuid_path(_: Request, x: uuid.UUID):
            return {}

        if ok.routes[0].params[0].kind != "uuid":
            failures.append(f"UUID path param registered as {ok.routes[0].params[0].kind!r}")
    except Exception as e:
        failures.append(f"UUID path param rejected: {type(e).__name__}: {e}")

    # An annotated argument outside the path is a query parameter, not an error.
    # This changed when query binding landed; see tests/query.py for coverage.
    try:
        ok = App()

        @ok.get("/a")
        async def h(_: Request, x: int = 1):
            return {}

        param = ok.routes[0].params[0]
        if (param.name, param.source) != ("x", "query"):
            failures.append(f"extra argument became {param.source} {param.name!r}")
    except Exception as e:
        failures.append(f"annotated extra argument rejected: {type(e).__name__}: {e}")
    return failures


def http_checks() -> list[str]:
    failures = []
    with httpx.Client(base_url=BASE, timeout=5) as client:
        for method, path, expect_status, expect_body in CASES:
            r = client.request(method, path)
            label = f"{method} {path}"
            if r.status_code != expect_status:
                failures.append(f"{label}: got {r.status_code}, expected {expect_status}")
                continue
            if expect_status == 405 and "allow" not in {k.lower() for k in r.headers}:
                failures.append(f"{label}: 405 without an Allow header")
            if expect_body is not None:
                body = r.json()
                for key, value in expect_body.items():
                    if body.get(key) != value:
                        failures.append(f"{label}: {key}={body.get(key)!r}, expected {value!r}")
    return failures


def main() -> None:
    failures = registration_checks()
    print(f"registration checks: {'PASS' if not failures else 'FAIL'}")

    threading.Thread(target=lambda: app.run(port=PORT), daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{BASE}/plain", timeout=0.3)
            break
        except Exception:
            threading.Event().wait(0.1)

    http_failures = http_checks()
    print(f"http checks ({len(CASES)} cases): {'PASS' if not http_failures else 'FAIL'}")
    failures += http_failures

    if failures:
        print("\nfailures:")
        for f in failures:
            print(f"  - {f}")
    print("\nRESULT:", "FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
