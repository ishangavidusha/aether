#!/usr/bin/env python3
"""What machine produced a number, and whether it was fit to produce one.

Every benchmark in this directory used to record the load average and nothing
else, because there was only ever one machine. Numbers from a second machine
cannot be compared to those without knowing what the second machine was, so
every result file now carries this fingerprint.

    bench/machine.py            # print the fingerprint and the preflight

Two jobs:

**Identity.** `fingerprint()` describes the host in enough detail to explain a
surprise later: CPU model, how many cores of which kind, memory, kernel,
whether it is virtualised, and what `aether._workers` made of all that. The
last one matters most — worker-count detection is the thing under test on any
machine that is not this one, and recording its answer beside the throughput is
how it gets checked rather than assumed.

**Fitness.** `preflight()` refuses to let a bad measurement look like a good
one. Four conditions make a number worthless, and all four are invisible in the
number itself:

* the CPU governor is `powersave`, so the cores never reach their real speed;
* the load average is already high, which reports regressions that do not exist;
* the host is stealing CPU from this guest, which is what a shared vCPU does;
* the file-descriptor limit is too low to open the connections being asked for.

`machine_id` is a hash of the specification, not of the hostname: two runs on
identically-specified hosts are meant to group together, and a host rebuilt
under a new name is still the same machine for comparison purposes.
"""
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: A load average above this depresses every number in the run. Chosen because
#: a phantom 3.5% regression on 2026-09-06 was entirely leftover benchmark load.
LOAD_CEILING = 2.0

#: Steal above this means the hypervisor is giving the CPU to someone else.
#: Anything above a fraction of a percent makes a shared vCPU host unusable for
#: measurement; the threshold is deliberately low.
STEAL_CEILING = 0.005

#: Descriptors needed per connection, plus headroom for the server's own files.
#: Streaming benchmarks open thousands, and the default limit on some hosts is
#: 1024, which fails as a timeout rather than as an error.
FD_FLOOR = 8192


def _read(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def _sysctl(name: str) -> str | None:
    if not sys.platform.startswith(("darwin", "freebsd")):
        return None
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _command(*argv: str) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def cpu_model() -> str | None:
    brand = _sysctl("machdep.cpu.brand_string")
    if brand:
        return brand
    info = _read("/proc/cpuinfo") or ""
    # Keyed rather than scanned line by line, and in priority order: x86 lists
    # a bare `model` (a stepping number) *before* `model name`, so taking the
    # first line that starts with "model" labels every Intel host with an
    # integer.
    fields = {}
    for line in info.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields.setdefault(key.strip().lower(), value.strip())
    for key in ("model name", "cpu model", "hardware", "machine"):
        if fields.get(key):
            return fields[key]

    # ARM kernels usually print no model name at all — an aarch64 /proc/cpuinfo
    # is a features list and a numeric implementer. lscpu decodes the numbers
    # against its own table and is present wherever util-linux is.
    for line in (_command("lscpu") or "").splitlines():
        if line.lower().startswith("model name:"):
            # lscpu answers "-" rather than nothing when its table has no entry
            # for the implementer, and "-" is worse than falling through.
            name = line.split(":", 1)[1].strip()
            if len(name) > 1:
                return name

    device_tree = _read("/sys/firmware/devicetree/base/model")
    if device_tree:
        return device_tree

    # Last resort: the raw identifiers. Not a name, but it distinguishes two
    # hosts, which is the whole reason this field exists.
    implementer = part = None
    for line in info.splitlines():
        if line.startswith("CPU implementer"):
            implementer = line.split(":", 1)[1].strip()
        elif line.startswith("CPU part"):
            part = line.split(":", 1)[1].strip()
    if implementer:
        return f"arm implementer {implementer} part {part or '?'}"
    return None


def memory_gb() -> float | None:
    total = _sysctl("hw.memsize")
    if total:
        return round(int(total) / 1024**3, 1)
    meminfo = _read("/proc/meminfo") or ""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            return round(int(line.split()[1]) / 1024**2, 1)
    return None


def governors() -> list[str]:
    """Every distinct scaling governor in use. Empty when the kernel has none.

    A cloud instance is often `powersave` out of the box, which on Intel
    hardware with the intel_pstate driver is not idle-only: it caps sustained
    frequency, and a benchmark measures the cap rather than the code.
    """
    found = set()
    root = Path("/sys/devices/system/cpu")
    if not root.is_dir():
        return []
    for path in sorted(root.glob("cpu[0-9]*/cpufreq/scaling_governor")):
        value = _read(str(path))
        if value:
            found.add(value)
    return sorted(found)


def virtualization() -> str | None:
    """Bare metal, a VM, or a container — and which, where it can be told."""
    detected = _command("systemd-detect-virt")
    if detected:
        return detected
    if Path("/.dockerenv").exists():
        return "docker"
    vendor = _read("/sys/class/dmi/id/sys_vendor")
    product = _read("/sys/class/dmi/id/product_name")
    if vendor or product:
        return " ".join(filter(None, (vendor, product)))
    if sys.platform == "darwin":
        return "none"
    return None


def fd_limit() -> int:
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return soft


def cpu_times() -> tuple[int, ...] | None:
    """The aggregate `cpu` line of /proc/stat, for a steal-time delta.

    Linux only. Steal cannot be measured on macOS and is not a question there:
    nothing else is competing for the cores.
    """
    stat = _read("/proc/stat")
    if not stat:
        return None
    first = stat.splitlines()[0].split()
    if first[0] != "cpu":
        return None
    return tuple(int(field) for field in first[1:])


def steal_fraction(before: tuple[int, ...] | None, after: tuple[int, ...] | None) -> float | None:
    """Share of the interval the hypervisor gave to another guest.

    Field 7 of the `cpu` line is `steal`. A dedicated CPU reports zero; a
    shared vCPU under contention reports several percent, and that contention
    is invisible in the throughput number it produced.
    """
    if not before or not after or len(before) < 8 or len(after) < 8:
        return None
    spent = sum(after) - sum(before)
    if spent <= 0:
        return None
    return (after[7] - before[7]) / spent


def worker_detection() -> dict | None:
    """What `aether._workers` concluded about this machine.

    Recorded on every run because it is itself untested anywhere but the
    machine it was written on: the hybrid-core probes have never seen a hybrid
    Linux host, and the cgroup probe has never seen a real quota.
    """
    try:
        from aether._workers import describe
    except Exception:
        return None
    try:
        return describe()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def python_build(executable: str | None = None) -> dict:
    if executable is None or Path(executable).resolve() == Path(sys.executable).resolve():
        gil = getattr(sys, "_is_gil_enabled", None)
        return {
            "executable": sys.executable,
            "version": platform.python_version(),
            "build": "gil" if (gil is None or gil()) else "free-threaded",
        }
    probe = (
        "import sys, platform;"
        "g = getattr(sys, '_is_gil_enabled', None);"
        "print(platform.python_version());"
        "print('gil' if (g is None or g()) else 'free-threaded')"
    )
    out = _command(executable, "-c", probe) or ""
    lines = out.splitlines()
    return {
        "executable": executable,
        "version": lines[0] if lines else None,
        "build": lines[1] if len(lines) > 1 else None,
    }


def git_commit() -> dict:
    head = _command("git", "-C", str(ROOT), "rev-parse", "--short", "HEAD")
    status = _command("git", "-C", str(ROOT), "status", "--porcelain")
    return {"commit": head, "dirty": bool(status)}


def toolchain() -> dict:
    rust = _command("rustc", "--version")
    oha = _command("oha", "--version")
    return {"rustc": rust, "oha": oha}


def fingerprint(executable: str | None = None) -> dict:
    """Everything worth knowing about the host, for one result file."""
    facts = {
        "os": f"{platform.system()} {platform.release()}",
        "kernel": platform.version(),
        "arch": platform.machine(),
        "hostname": platform.node(),
        "cpu_model": cpu_model(),
        "logical_cpus": os.cpu_count(),
        "memory_gb": memory_gb(),
        "virtualization": virtualization(),
        "governors": governors(),
        "fd_limit": fd_limit(),
        "loadavg": list(os.getloadavg()),
        "python": python_build(executable),
        "aether": git_commit(),
        "toolchain": toolchain(),
        "worker_detection": worker_detection(),
    }
    facts["machine_id"] = machine_id(facts)
    return facts


def machine_id(facts: dict) -> str:
    """A short stable key for the specification, not for the host.

    Two identically-specified hosts share an id on purpose, so a droplet
    destroyed and recreated at the same size still compares against its own
    earlier numbers. The hostname is recorded but deliberately excluded.
    """
    detected = (facts.get("worker_detection") or {}).get("detected")
    spec = json.dumps(
        [
            facts.get("os", "").split()[0],
            facts.get("arch"),
            facts.get("cpu_model"),
            facts.get("logical_cpus"),
            facts.get("memory_gb"),
            facts.get("virtualization"),
            detected,
        ],
        sort_keys=True,
    )
    digest = hashlib.sha256(spec.encode()).hexdigest()[:6]
    cpus = facts.get("logical_cpus") or 0
    system = (facts.get("os") or "unknown").split()[0].lower()
    return f"{system}-{facts.get('arch', 'unknown')}-{cpus}c-{digest}"


def preflight(facts: dict, conns: int = 64) -> list[str]:
    """Reasons the numbers this host is about to produce cannot be trusted."""
    problems = []

    bad = [g for g in facts.get("governors", []) if g not in ("performance", "schedutil")]
    if bad:
        problems.append(
            f"CPU governor is {', '.join(bad)}; the cores will not reach their rated speed. "
            f"Set it with: echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
        )

    load = (facts.get("loadavg") or [0])[0]
    if load > LOAD_CEILING:
        problems.append(
            f"1-minute load average is {load:.1f}, above {LOAD_CEILING}. Wait for the machine to "
            f"go quiet; leftover load reports regressions that do not exist."
        )

    limit = facts.get("fd_limit") or 0
    if limit < max(FD_FLOOR, conns * 4):
        problems.append(
            f"file-descriptor limit is {limit}, too low for {conns} connections with headroom. "
            f"Raise it with: ulimit -n {FD_FLOOR}"
        )

    if facts.get("python", {}).get("build") is None:
        problems.append("could not determine whether the interpreter is free-threaded")

    return problems


def steal_check(fraction: float | None) -> str | None:
    """The one condition that can only be measured across the run itself."""
    if fraction is None or fraction <= STEAL_CEILING:
        return None
    return (
        f"the hypervisor stole {fraction * 100:.1f}% of the CPU during this run. "
        f"This is a shared vCPU host; the numbers are not reproducible. Measure on a "
        f"dedicated-CPU instance instead."
    )


def summarise(facts: dict) -> str:
    cores = facts.get("logical_cpus")
    detection = facts.get("worker_detection") or {}
    detected = detection.get("detected")
    quota = detection.get("cgroup_quota")
    # os.cpu_count() reports the host's cores inside a container, so saying
    # "10 cpus" on a two-CPU quota would be the opposite of informative.
    seen = f"{cores} cpus" if not quota else f"{cores} cpus, quota {quota}"
    parts = [
        facts.get("machine_id", "?"),
        facts.get("cpu_model") or facts.get("arch") or "?",
        seen + (f", {detected} usable for python" if detected else ""),
        f"{facts.get('memory_gb')} GiB",
        facts.get("virtualization") or "unknown virtualisation",
    ]
    return " · ".join(str(p) for p in parts)


def worker_ladder(limit: int | None = None) -> list[int]:
    """Worker-loop counts worth measuring on this machine.

    Powers of two up to the CPU count, plus the CPU count itself when it is not
    one. A fixed `[1, 2, 4, 8]` was right on a ten-core laptop and is exactly
    wrong on a 32-core host, where the open question is whether the cap of 8
    should be higher — a ladder that stops at 8 cannot answer it.
    """
    top = limit or os.cpu_count() or 1
    ladder = []
    step = 1
    while step <= top:
        ladder.append(step)
        step *= 2
    if ladder and ladder[-1] != top:
        ladder.append(top)
    return ladder or [1]


class Session:
    """One benchmark run, wrapped so every runner records the same envelope.

    Captures the host up front, prints it, refuses to continue quietly when the
    host is unfit, measures steal across the whole run, and writes a result file
    named for the machine rather than only for the clock. Three runners used to
    do four different subsets of this.
    """

    def __init__(self, kind: str, executable: str | None = None, conns: int = 64,
                 strict: bool = False) -> None:
        self.kind = kind
        self.facts = fingerprint(executable)
        self.problems = preflight(self.facts, conns)
        self.started = cpu_times()
        self.strict = strict

    def announce(self) -> None:
        print(summarise(self.facts))
        python = self.facts["python"]
        print(f"python: {python['version']} {python['build']}   "
              f"aether: {self.facts['aether']['commit']}"
              f"{' (dirty)' if self.facts['aether']['dirty'] else ''}")
        # Flush before writing to stderr, or the warnings land above the
        # summary they are about when the two streams are buffered differently.
        sys.stdout.flush()
        for problem in self.problems:
            print(f"  ! {problem}", file=sys.stderr)
        if self.problems and self.strict:
            print("\nrefusing to measure on an unfit host (--strict)", file=sys.stderr)
            sys.exit(3)
        print()

    def finish(self, payload: dict) -> Path:
        """Write the result file and report anything that spoiled the run."""
        stolen = steal_fraction(self.started, cpu_times())
        complaint = steal_check(stolen)
        sys.stdout.flush()
        if complaint:
            print(f"\n  ! {complaint}", file=sys.stderr)
        self.facts["loadavg_after"] = list(os.getloadavg())

        build = self.facts["python"]["build"]
        tag = "ft" if build == "free-threaded" else "gil"
        results = ROOT / "bench" / "results"
        results.mkdir(parents=True, exist_ok=True)
        out = results / f"{self.kind}-{self.facts['machine_id']}-{tag}-{stamp()}.json"
        out.write_text(json.dumps({
            "kind": self.kind,
            "machine": self.facts,
            "preflight": self.problems,
            "steal_fraction": stolen,
            "trustworthy": not self.problems and complaint is None,
            **payload,
        }, indent=2))
        return out


def stamp() -> str:
    import time

    return time.strftime("%Y%m%d-%H%M%S")


def main() -> None:
    facts = fingerprint()
    if "--json" in sys.argv:
        # For a caller that wants the facts of a host it cannot import from,
        # such as the inside of a container.
        print(json.dumps(facts))
        return
    print(summarise(facts))
    print()
    print(json.dumps(facts, indent=2))
    problems = preflight(facts)
    if problems:
        print("\npreflight:", file=sys.stderr)
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
    else:
        print("\npreflight: ok")


if __name__ == "__main__":
    main()
