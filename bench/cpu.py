#!/usr/bin/env python3
"""Measure handler-execution parallelism as worker loops increase.

    bench/cpu.py --python .venv/bin/python       # free-threaded
    bench/cpu.py --python .venv-gil/bin/python   # GIL

On a GIL build throughput should stay flat no matter how many loops run,
because only one thread executes Python at a time. On a free-threaded build it
should climb until it saturates the performance cores.
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
PORT = 8767


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


def measure(py, workers, duration, conns):
    cmd = [py, "bench/cpu_app.py", "--port", str(PORT), "--workers", str(workers)]
    proc = subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                            start_new_session=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
    try:
        if not wait_port(proc):
            print(f"  !! {workers}w failed to start: {proc.stderr.read()[-500:]}", file=sys.stderr)
            return None
        url = f"http://127.0.0.1:{PORT}/cpu"
        for secs in (2, duration):
            out = subprocess.run(
                ["oha", "--no-tui", "--output-format", "json", "-z", f"{secs}s", "-c", str(conns), url],
                capture_output=True, text=True, check=True).stdout
        r = json.loads(out)
        return {"workers": workers, "rps": r["summary"]["requestsPerSec"],
                "p50_ms": r["latencyPercentiles"]["p50"] * 1000,
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
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--duration", type=int, default=6)
    ap.add_argument("--conns", type=int, default=32)
    ap.add_argument("--workers", type=int, nargs="*", default=[1, 2, 4, 8])
    args = ap.parse_args()

    py = str(Path(args.python).absolute())
    info = subprocess.run(
        [py, "-c", "import sys;print('gil' if sys._is_gil_enabled() else 'free-threaded')"],
        capture_output=True, text=True, check=True).stdout.strip()
    print(f"build: {info}   load: {args.conns} conns x {args.duration}s   handler: 20k-iteration loop\n")

    rows = [r for w in args.workers if (r := measure(py, w, args.duration, args.conns))]
    base = rows[0]["rps"] if rows else 1
    print(f"{'loops':>6}{'req/s':>10}{'speedup':>10}{'p50 ms':>10}{'p99 ms':>10}")
    for r in rows:
        print(f"{r['workers']:>6}{r['rps']:>10.0f}{r['rps'] / base:>9.2f}x{r['p50_ms']:>10.2f}{r['p99_ms']:>10.2f}")

    out = ROOT / "bench" / "results" / f"cpu-{'ft' if info != 'gil' else 'gil'}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"build": info, "rows": rows}, indent=2))
    print(f"\nsaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
