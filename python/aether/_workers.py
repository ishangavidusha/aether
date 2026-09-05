"""Choosing how many Python worker loops to start.

Measured on 2026-09-06 (`bench/sweep.py`, handler CPU cost against loop count):

* On a GIL build, one loop is best or tied at every handler cost, and extra
  loops only cost throughput. So: one loop, always.
* On a free-threaded build, one loop is *never* best. Even a handler that does
  nothing gains from more loops, and a handler doing 500us of work gains 3.4x.
  Throughput peaks at the performance-core count and falls off when the
  efficiency cores get oversubscribed.

So the target is "how many cores can actually run Python in parallel", which is
not `os.cpu_count()`. That number counts efficiency cores, and inside a
container it reports the host's cores rather than the cgroup limit, which would
start dozens of loops for a two-CPU quota.

Every probe below is best-effort and returns None when it cannot answer. The
final answer is the most constrained of everything that did answer.
"""

import os
import sys

# Guard against a probe returning something absurd, not a tuning limit. Raise it
# with `workers=` for a CPU-heavy service on a large homogeneous machine.
MAX_DEFAULT_WORKERS = 8


def gil_enabled() -> bool:
    probe = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else bool(probe())


def _sysctl_int(name: str) -> int | None:
    """Read an integer sysctl on macOS or BSD without shelling out."""
    if not sys.platform.startswith(("darwin", "freebsd")):
        return None
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.util.find_library("c")
        if lib is None:
            return None
        libc = ctypes.CDLL(lib, use_errno=True)
        value = ctypes.c_int64(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        rc = libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0)
        return int(value.value) if rc == 0 and value.value > 0 else None
    except Exception:
        return None


def _performance_cores() -> int | None:
    """Cores that run at full speed. Apple Silicon splits these from the
    efficiency cores, and the sweep showed loops on efficiency cores losing
    throughput rather than adding it."""
    return _sysctl_int("hw.perflevel0.physicalcpu")


def _physical_cores() -> int | None:
    """Physical cores, ignoring SMT siblings. Two hyperthreads on one core do
    not give two loops' worth of parallel Python."""
    macos = _sysctl_int("hw.physicalcpu")
    if macos:
        return macos
    try:
        import glob

        pairs = set()
        for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology"):
            try:
                with open(f"{path}/core_id") as f:
                    core = f.read().strip()
                with open(f"{path}/physical_package_id") as f:
                    package = f.read().strip()
                pairs.add((package, core))
            except OSError:
                continue
        return len(pairs) or None
    except Exception:
        return None


def _cgroup_quota() -> int | None:
    """CPU quota of the current container, rounded up. `os.cpu_count()` does not
    see this, which is how a 2-CPU container on a 64-core host ends up starting
    64 event loops."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:  # cgroup v2
            quota, period = f.read().split()
            if quota != "max":
                return max(1, -(-int(quota) // int(period)))
            return None
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:  # cgroup v1
            quota = int(f.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
            period = int(f.read().strip())
        if quota > 0 and period > 0:
            return max(1, -(-quota // period))
    except (OSError, ValueError):
        pass
    return None


def _available_cpus() -> int:
    """Respects CPU affinity where the platform reports it."""
    counter = getattr(os, "process_cpu_count", None)
    return (counter() if counter else None) or os.cpu_count() or 1


def detect_parallelism() -> int:
    """How many loops can genuinely run Python at the same time."""
    limits = [
        limit
        for limit in (_performance_cores(), _physical_cores(), _cgroup_quota())
        if limit
    ]
    limits.append(_available_cpus())
    return max(1, min(limits))


def default_workers() -> int:
    if gil_enabled():
        return 1
    return max(1, min(MAX_DEFAULT_WORKERS, detect_parallelism()))


def describe() -> dict[str, int | None]:
    """Everything the probes found. For diagnostics and tests."""
    return {
        "performance_cores": _performance_cores(),
        "physical_cores": _physical_cores(),
        "cgroup_quota": _cgroup_quota(),
        "available_cpus": _available_cpus(),
        "os_cpu_count": os.cpu_count(),
        "detected": detect_parallelism(),
        "default_workers": default_workers(),
    }
