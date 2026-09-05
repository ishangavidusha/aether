#!/usr/bin/env python3
"""Sanity checks for worker-count detection.

Cannot assert exact numbers, since they are machine specific. Asserts the
invariants that would actually break a deployment.
"""
import sys

from aether import _workers


def main() -> None:
    info = _workers.describe()
    gil = _workers.gil_enabled()

    print(f"build             : {'GIL' if gil else 'free-threaded'}")
    for key, value in info.items():
        print(f"{key:<18}: {value}")

    failures = []
    workers = info["default_workers"]

    if gil and workers != 1:
        failures.append(f"GIL build must use exactly 1 loop, got {workers}")
    if workers < 1:
        failures.append(f"worker count must be at least 1, got {workers}")
    if workers > _workers.MAX_DEFAULT_WORKERS:
        failures.append(f"{workers} exceeds the cap of {_workers.MAX_DEFAULT_WORKERS}")
    if info["detected"] > info["available_cpus"]:
        failures.append(
            f"detected {info['detected']} exceeds {info['available_cpus']} available CPUs"
        )
    if not gil and workers > info["detected"]:
        failures.append(f"{workers} loops exceeds detected parallelism {info['detected']}")
    perf = info["performance_cores"]
    if perf and info["detected"] > perf:
        failures.append(f"detected {info['detected']} exceeds {perf} performance cores")

    print("\nRESULT:", "FAIL - " + "; ".join(failures) if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
