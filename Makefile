# Aether milestone-1 spike. Two venvs: .venv (free-threaded 3.14t) and .venv-gil (standard 3.14).
FT_PY  := 3.14.7+freethreaded
GIL_PY := /opt/homebrew/bin/python3.14

.PHONY: venvs build build-ft build-gil run bench bench-gil bench-cpu bench-cpu-gil sweep sweep-gil verify verify-gil up down logs image stack stack-down clean

venvs:
	uv venv --python $(FT_PY) .venv
	uv venv --python $(GIL_PY) .venv-gil
	uv pip install --python .venv/bin/python maturin uvicorn granian fastapi httpx openapi-spec-validator websockets redis mcp
	uv pip install --python .venv-gil/bin/python maturin uvicorn granian fastapi httpx openapi-spec-validator websockets redis mcp

build: build-ft build-gil

build-ft:
	VIRTUAL_ENV=$(CURDIR)/.venv .venv/bin/maturin develop --release

build-gil:
	VIRTUAL_ENV=$(CURDIR)/.venv-gil .venv-gil/bin/maturin develop --release

run: build-ft
	.venv/bin/python examples/hello.py

bench: build-ft
	.venv/bin/python bench/run.py --python .venv/bin/python

bench-gil: build-gil
	.venv-gil/bin/python bench/run.py --python .venv-gil/bin/python

bench-cpu: build-ft
	.venv/bin/python bench/cpu.py --python .venv/bin/python

bench-cpu-gil: build-gil
	.venv-gil/bin/python bench/cpu.py --python .venv-gil/bin/python

verify: build-ft
	.venv/bin/python tests/workers.py
	.venv/bin/python tests/routing.py
	.venv/bin/python tests/query.py
	.venv/bin/python tests/bodies.py
	.venv/bin/python tests/openapi.py
	.venv/bin/python tests/capabilities.py
	.venv/bin/python tests/streams.py
	.venv/bin/python tests/sse.py
	.venv/bin/python tests/websocket.py
	.venv/bin/python tests/hardening.py
	.venv/bin/python tests/durable.py
	.venv/bin/python tests/backpressure.py
	.venv/bin/python tests/verify.py

verify-gil: build-gil
	.venv-gil/bin/python tests/workers.py
	.venv-gil/bin/python tests/routing.py
	.venv-gil/bin/python tests/query.py
	.venv-gil/bin/python tests/bodies.py
	.venv-gil/bin/python tests/openapi.py
	.venv-gil/bin/python tests/capabilities.py
	.venv-gil/bin/python tests/streams.py
	.venv-gil/bin/python tests/sse.py
	.venv-gil/bin/python tests/websocket.py
	.venv-gil/bin/python tests/hardening.py
	.venv-gil/bin/python tests/durable.py
	.venv-gil/bin/python tests/backpressure.py
	.venv-gil/bin/python tests/verify.py

sweep: build-ft
	.venv/bin/python bench/sweep.py --python .venv/bin/python

sweep-gil: build-gil
	.venv-gil/bin/python bench/sweep.py --python .venv-gil/bin/python

# --- containers -------------------------------------------------------------
# Services run in containers so nothing has to be installed on the host.
# Docker Desktop does not always put its CLI on a non-interactive PATH.
DOCKER := $(shell command -v docker 2>/dev/null || echo $(HOME)/.docker/bin/docker)
COMPOSE := $(DOCKER) compose

# Durable-topic tests need Redis. Without it they print SKIP and still pass, so
# run this before trusting `make verify` to have covered milestone 4.
up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f

# Build the app image, and run a two-node stack against one Redis. This is the
# only way to exercise cross-process fan-out the way it actually ships.
image:
	$(DOCKER) build -t aether:dev .

stack: image
	$(COMPOSE) -f docker-compose.yml -f docker-compose.stack.yml up -d --wait

stack-down:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.stack.yml down -v

clean:
	rm -rf target .venv .venv-gil bench/results
