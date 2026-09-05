# Aether milestone-1 spike. Two venvs: .venv (free-threaded 3.14t) and .venv-gil (standard 3.14).
FT_PY  := 3.14.7+freethreaded
GIL_PY := /opt/homebrew/bin/python3.14

.PHONY: venvs build build-ft build-gil run bench bench-gil bench-cpu bench-cpu-gil sweep sweep-gil verify verify-gil clean

venvs:
	uv venv --python $(FT_PY) .venv
	uv venv --python $(GIL_PY) .venv-gil
	uv pip install --python .venv/bin/python maturin uvicorn granian fastapi httpx
	uv pip install --python .venv-gil/bin/python maturin uvicorn granian fastapi httpx

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
	.venv/bin/python tests/bodies.py
	.venv/bin/python tests/backpressure.py
	.venv/bin/python tests/verify.py

verify-gil: build-gil
	.venv-gil/bin/python tests/workers.py
	.venv-gil/bin/python tests/routing.py
	.venv-gil/bin/python tests/bodies.py
	.venv-gil/bin/python tests/backpressure.py
	.venv-gil/bin/python tests/verify.py

sweep: build-ft
	.venv/bin/python bench/sweep.py --python .venv/bin/python

sweep-gil: build-gil
	.venv-gil/bin/python bench/sweep.py --python .venv-gil/bin/python

clean:
	rm -rf target .venv .venv-gil bench/results
