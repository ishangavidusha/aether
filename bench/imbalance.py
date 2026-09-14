#!/usr/bin/env python3
"""Does round-robin send requests to a worker loop that is busy? (I-018)

    bench/imbalance.py --python .venv/bin/python

Worker assignment is round-robin, and only moves on when a worker's queue is
full. A handler that computes rather than awaits holds its loop for as long as
it runs, and every request assigned to that loop meanwhile waits behind it —
even when the other loops are idle.

This measures what that costs a *fast* route. A fixed-rate stream of `/fast`
requests runs alone, then alongside a steady number of concurrent `/cpu`
requests that each hold a loop, then alongside `/io` requests that wait the same
time without holding one. `/io` is the control: if it hurts `/fast`, the cost is
something other than a blocked loop.

The fast stream is rate-limited with latency correction, so a stalled request
counts from when it *should* have been sent. Without that, a closed-loop load
generator waits politely for the slow response and under-reports exactly the
delay under test (coordinated omission).
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

# bench/ is a directory of scripts rather than a package, so a sibling
# import needs the directory on the path first.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from machine import Session

ROOT = Path(__file__).resolve().parent.parent
PORT = 8771
PERCENTILES = ("p50", "p90", "p99", "p99.9")


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


def oha_args(path: str, seconds: float, conns: int, rate: int | None) -> list[str]:
    args = ["oha", "--no-tui", "--output-format", "json", "-z", f"{seconds}s",
            "-c", str(conns)]
    if rate:
        args += ["-q", str(rate), "--latency-correction"]
    return args + [f"http://127.0.0.1:{PORT}{path}"]


def scenario(label, slow_path, slow_conns, args):
    """One fast stream, optionally with a slow stream running underneath it."""
    background = None
    if slow_path:
        # Started first and outliving the fast stream, so the fast stream only
        # ever sees steady-state background load.
        background = subprocess.Popen(
            oha_args(slow_path, args.duration + 2, slow_conns, None),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        time.sleep(1.0)

    fast = json.loads(subprocess.run(
        oha_args("/fast", args.duration, args.conns, args.rate),
        capture_output=True, text=True, check=True).stdout)

    slow = None
    if background:
        out, _ = background.communicate(timeout=args.duration + 30)
        slow = json.loads(out)

    lat = fast["latencyPercentiles"]
    row = {
        "scenario": label,
        "slow_path": slow_path,
        "slow_concurrency": slow_conns,
        "fast_rps": fast["summary"]["requestsPerSec"],
        "fast_success": fast["summary"]["successRate"],
        **{f"fast_{p}_ms": (lat.get(p) or 0) * 1000 for p in PERCENTILES},
        "slow_rps": slow["summary"]["requestsPerSec"] if slow else None,
    }
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--duration", type=int, default=8)
    ap.add_argument("--conns", type=int, default=32)
    ap.add_argument("--rate", type=int, default=2000, help="fast requests per second")
    ap.add_argument("--slow-ms", type=int, default=50, help="how long a slow request holds")
    ap.add_argument("--strict", action="store_true",
                    help="refuse to measure when the host fails preflight")
    args = ap.parse_args()

    py = str(Path(args.python).absolute())
    session = Session("imbalance", executable=py, conns=args.conns, strict=args.strict)
    session.announce()
    if session.facts["python"]["build"] != "free-threaded":
        print("the GIL build runs one worker loop, so there is no assignment to measure")
        sys.exit(2)

    env = {**os.environ, "PYTHONPATH": str(ROOT), "OXBROOK_SLOW_MS": str(args.slow_ms)}
    proc = subprocess.Popen(
        [py, "bench/imbalance_app.py", "--port", str(PORT), "--workers", str(args.workers)],
        cwd=ROOT, env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    scenarios = [("fast alone", None, 0)]
    scenarios += [(f"+{k} cpu-bound", "/cpu", k) for k in (1, 2, args.workers - 1)]
    scenarios += [(f"+{args.workers} io-bound", "/io", args.workers)]

    rows = []
    try:
        if not wait_port(proc):
            print(f"server failed to start: {(proc.stderr.read() or '')[-600:]}", file=sys.stderr)
            sys.exit(1)
        # One throwaway pass, so the first scenario is not also measuring warmup.
        subprocess.run(oha_args("/fast", 2, args.conns, None), capture_output=True, check=True)

        print(f"{args.workers} loops, fast stream {args.rate} req/s over {args.conns} conns, "
              f"slow requests hold {args.slow_ms} ms\n")
        header = "".join(f"{p + ' ms':>10}" for p in PERCENTILES)
        print(f"{'scenario':<18}{header}{'ok%':>8}")
        for label, path, k in scenarios:
            row = scenario(label, path, k, args)
            rows.append(row)
            cells = "".join(f"{row[f'fast_{p}_ms']:>10.2f}" for p in PERCENTILES)
            print(f"{label:<18}{cells}{row['fast_success'] * 100:>8.1f}", flush=True)
            time.sleep(1.0)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)

    # What a blocked loop predicts, to compare against rather than eyeball: with
    # k of N loops held, about k/N of fast requests land behind one and wait a
    # uniform share of the hold time.
    print("\nprediction if assignment ignores busy loops: with k of "
          f"{args.workers} loops held, the slowest k/{args.workers} of fast requests "
          f"wait up to {args.slow_ms} ms")

    out = session.finish({"args": vars(args), "rows": rows})
    print(f"saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
