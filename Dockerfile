# Aether in a container.
#
# The official Python images have no free-threaded interpreter, so uv installs
# 3.14t here exactly as it does on a development machine. That keeps the
# container and the host on the same interpreter rather than quietly testing a
# different one.

# ---------- build the extension ----------
FROM rust:1-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.14t

WORKDIR /src
COPY Cargo.toml Cargo.lock pyproject.toml ./
COPY src ./src
COPY python ./python

# Cache mounts keep a rebuild to the crates that actually changed; a cold
# build compiles the whole dependency tree with fat LTO and is slow.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    uv tool run --from 'maturin>=1.9,<2' maturin build \
        --release --out /dist --interpreter "$(uv python find 3.14t)"

# ---------- runtime ----------
FROM debian:bookworm-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
RUN uv python install 3.14t && uv venv --python 3.14t /app/.venv

COPY --from=builder /dist/*.whl /tmp/
RUN uv pip install --python /app/.venv/bin/python /tmp/*.whl "redis>=5" \
    && rm -rf /tmp/*.whl

WORKDIR /app
COPY examples ./examples

# Anything that binds only to loopback is unreachable from outside the
# container, so apps here must listen on 0.0.0.0.
EXPOSE 8000
CMD ["python", "-c", "import aether, sys; print('aether ready on', sys.version)"]
