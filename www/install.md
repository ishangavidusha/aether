# Install

Aether is not published to PyPI. Build it from the repository.

## Requirements

- **Python 3.13 or newer.** Free-threaded CPython 3.14 (`python3.14t`) is the
  primary target. The standard GIL build works, with a single worker loop.
- **A Rust toolchain**, to build the extension module.
- **[uv](https://docs.astral.sh/uv/)**, for the virtual environments.
- **Docker**, only if you want durable topics: Redis runs in a container.
- **[oha](https://github.com/hatoo/oha)**, only if you want to run the benchmarks.

## Build

```bash
make venvs     # .venv (free-threaded 3.14t) and .venv-gil (standard 3.14)
make build     # maturin develop --release into both
make run       # examples/hello.py
```

`make build` compiles the Rust crate into `aether._core` and installs the
package into both environments. Rebuild after any change to `src/`; the `make`
targets that need it already do.

To build into one environment only:

```bash
maturin develop --release
```

## First app

```python
from aether import App, Request

app = App(title="Notes", version="1.0.0")

@app.get("/notes/{note_id}")
async def read_note(_: Request, note_id: int):
    """Read one note by its id."""
    return {"id": note_id, "title": "hello"}

if __name__ == "__main__":
    app.run(port=8000)
```

```bash
python app.py
```

That gives you, without further configuration:

- `GET /notes/1` returning JSON, and `HEAD` on the same path
- `422` for `GET /notes/abc`, produced in Rust before Python is woken
- `405` with an `Allow` header for `DELETE /notes/1`
- `GET /openapi.json` and a documentation page at `GET /docs`
- `POST /mcp` speaking the Model Context Protocol, exposing nothing until a
  route asks to be exposed

## Services

Services run in containers rather than on the host.

```bash
make up      # start redis
make down    # stop it and remove its volume
make stack   # build the app image and run two nodes against one redis
```

## Handler rules

Handlers are `async def`. A synchronous handler is a `TypeError` at
registration rather than a surprise at runtime, because a blocking call on a
worker loop stalls every request that loop is carrying.

```python
@app.get("/bad")
def wrong(_: Request):      # TypeError: handlers must be async def
    return {}
```
