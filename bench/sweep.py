#!/usr/bin/env python3
"""Find where extra worker loops start paying for themselves.

Sweeps handler CPU cost against worker-loop count. The hello-world benchmark
(zero handler cost) prefers few loops because dispatch dominates and wakeups
coalesce. The CPU-bound benchmark prefers many. This locates the crossover, so
the default worker count can be chosen from data instead of from an argument.

    bench/sweep.py --python .venv/bin/python
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8769

CALIBRATE = """
import time

def work(n):
    total = 0
    for i in range(n):
        total += i * i
    return total

work(200_000)  # warm up
n = 2_000_000
times = []
for _ in range(5):
    start = time.perf_counter()
    work(n)
    times.append(time.perf_counter() - start)
print(min(times) / n * 1e9)
"""


def calibrate(py: str) -> float:
    """Nanoseconds per loop iteration on this interpreter."""
    out = subprocess.run([py, "-c", CALIBRATE], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def wait_port(proc, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def measure(py, iters, workers, duration, conns):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "AETHER_SWEEP_ITERS": str(iters)}
    cmd = [py, "bench/sweep_app.py", "--port", str(PORT), "--workers", str(workers)]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        if not wait_port(proc):
            print(f"  !! iters={iters} workers={workers} failed: "
                  f"{(proc.stderr.read() or '')[-400:]}", file=sys.stderr)
            return None
        url = f"http://127.0.0.1:{PORT}/work"
        for secs in (2, duration):
            out = subprocess.run(
                ["oha", "--no-tui", "--output-format", "json", "-z", f"{secs}s",
                 "-c", str(conns), url],
                capture_output=True, text=True, check=True).stdout
        r = json.loads(out)
        return {"rps": r["summary"]["requestsPerSec"],
                "p99_ms": r["latencyPercentiles"]["p99"] * 1000}
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGINT)
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(0.4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--duration", type=int, default=5)
    ap.add_argument("--conns", type=int, default=64)
    ap.add_argument("--loops", type=int, nargs="*", default=[1, 2, 4, 8])
    ap.add_argument("--costs", type=float, nargs="*",
                    default=[0, 5, 10, 25, 50, 100, 250, 500],
                    help="target handler CPU cost in microseconds")
    args = ap.parse_args()

    py = str(Path(args.python).absolute())
    build = subprocess.run(
        [py, "-c", "import sys;print('gil' if sys._is_gil_enabled() else 'free-threaded')"],
        capture_output=True, text=True, check=True).stdout.strip()

    load_before = os.getloadavg()
    if load_before[0] > 2.0:
        print(f"WARNING: 1-minute load average is {load_before[0]:.1f}; results will be "
              f"depressed.\n", file=sys.stderr)

    ns = calibrate(py)
    print(f"build: {build}   load: {args.conns} conns x {args.duration}s")
    print(f"calibration: {ns:.2f} ns per loop iteration\n")

    grid, rows = {}, []
    for cost in args.costs:
        iters = int(cost * 1000 / ns)
        actual = iters * ns / 1000
        for loops in args.loops:
            r = measure(py, iters, loops, args.duration, args.conns)
            if r:
                grid[(cost, loops)] = r["rps"]
                rows.append({"cost_us": cost, "actual_us": actual, "iters": iters,
                             "loops": loops, **r})
        got = " ".join(f"{loops}L={grid.get((cost, loops), 0):>8.0f}" for loops in args.loops)
        print(f"  handler ~{actual:>6.1f}us (iters={iters:>6})  {got}", flush=True)

    header = "".join(f"{f'{n} loop' + ('s' if n > 1 else ''):>12}" for n in args.loops)
    print(f"\n{'handler µs':>11}{header}{'best':>8}{'gain':>8}")
    for cost in args.costs:
        vals = [grid.get((cost, n)) for n in args.loops]
        if None in vals:
            continue
        base = vals[0]
        best_i = max(range(len(vals)), key=lambda i: vals[i])
        cells = "".join(f"{v:>12.0f}" for v in vals)
        print(f"{cost:>11.0f}{cells}{args.loops[best_i]:>7}L{vals[best_i] / base:>7.2f}x")

    crossover = None
    for cost in args.costs:
        vals = [grid.get((cost, n)) for n in args.loops]
        if None in vals:
            continue
        if max(vals[1:]) > vals[0] * 1.05:
            crossover = cost
            break
    print(f"\ncrossover: extra loops first win by >5% at a handler cost of "
          f"{'~%.0f us' % crossover if crossover is not None else 'never (in this range)'}")

    out = ROOT / "bench" / "results" / f"sweep-{'ft' if build != 'gil' else 'gil'}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"build": build, "ns_per_iter": ns,
                               "loadavg_before": load_before,
                               "crossover_us": crossover, "rows": rows}, indent=2))
    print(f"saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
