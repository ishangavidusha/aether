# Aether milestone-1 spike. Two venvs: .venv (free-threaded 3.14t) and .venv-gil (standard 3.14).
FT_PY  := 3.14.7+freethreaded
GIL_PY := /opt/homebrew/bin/python3.14

.PHONY: venvs build build-ft build-gil docs docs-serve coverage run bench bench-gil bench-cpu bench-cpu-gil sweep sweep-gil verify verify-gil up down logs image stack stack-down clean

venvs:
	uv venv --python $(FT_PY) .venv
	uv venv --python $(GIL_PY) .venv-gil
	uv pip install --python .venv/bin/python maturin uvicorn granian fastapi httpx openapi-spec-validator websockets redis mcp coverage
	uv pip install --python .venv-gil/bin/python maturin uvicorn granian fastapi httpx openapi-spec-validator websockets redis mcp coverage
	# Docs tooling only in the GIL venv: mkdocs has no reason to run twice.
	uv pip install --python .venv-gil/bin/python mkdocs-material 'mkdocstrings[python]' ruff

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

# One list, used by both builds and by the coverage run. Three copies of it is
# how a suite ends up running on one interpreter and not the other.
SUITES := workers routing query bodies openapi capabilities streams sse \
          websocket hardening plumbing injection durable backpressure verify

# SUITE_TIMEOUT is empty locally and set to `timeout 300` in CI, where a hung
# suite would otherwise burn the whole job. Echo the name first: a suite that
# hangs before its own first print is otherwise invisible in a CI log.
SUITE_TIMEOUT ?=

verify: build-ft
	@for s in $(SUITES); do echo "== $$s"; $(SUITE_TIMEOUT) .venv/bin/python tests/$$s.py || exit 1; done

verify-gil: build-gil
	@for s in $(SUITES); do echo "== $$s"; $(SUITE_TIMEOUT) .venv-gil/bin/python tests/$$s.py || exit 1; done

# Branch coverage of the Python half. COVERAGE_CORE=sysmon matters: handlers run
# on threads Rust created, which the classic trace hook never sees, and the
# report would understate the runtime by a wide margin.
COVERAGE_MIN := 85

coverage: build-ft
	@rm -f .coverage .coverage.[0-9]* 2>/dev/null || true
	@for s in $(SUITES); do echo "== $$s"; \
		COVERAGE_CORE=sysmon $(SUITE_TIMEOUT) .venv/bin/python -m coverage run --branch -p \
			--source=python/aether tests/$$s.py || exit 1; \
	done
	@.venv/bin/python -m coverage combine -q
	@.venv/bin/python -m coverage report --precision=1 --sort=cover \
		--fail-under=$(COVERAGE_MIN)
	@.venv/bin/python -m coverage html -q -d htmlcov
	@echo "html report: htmlcov/index.html"

# --- public documentation ---------------------------------------------------
# Built from the GIL venv, which is where the docs tooling lives. mkdocstrings
# imports the package for the API reference, so the extension has to be built
# first: an unbuilt tree documents nothing.
docs: build-gil
	.venv-gil/bin/python -m mkdocs build --strict

docs-serve: build-gil
	.venv-gil/bin/python -m mkdocs serve

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
	rm -rf target .venv .venv-gil bench/results site htmlcov
