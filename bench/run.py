#!/usr/bin/env python3
"""Benchmark Aether against uvicorn / granian / FastAPI with `oha`.

    bench/run.py --python .venv/bin/python            # free-threaded
    bench/run.py --python .venv-gil/bin/python        # GIL build
    bench/run.py --python .venv/bin/python aether uvicorn-raw

Each target is started as a subprocess, warmed up, measured, and killed.
Results are printed as a table and saved to bench/results/<name>.json.
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
RESULTS = ROOT / "bench" / "results"
PORT = 8765


POST_JSON = ["-m", "POST", "-H", "content-type: application/json",
             "-d", '{"name":"ada","age":36,"email":"ada@example.com"}']


def target_cmds(py: str, workers: int) -> dict[str, tuple[list[str], str, list[str]]]:
    """name -> (command, request path, extra oha args)"""
    uv = [py, "-m", "uvicorn", "--host", "127.0.0.1", "--port", str(PORT),
          "--log-level", "warning", "--loop", "asyncio", "--http", "h11"]
    gr = [py, "-m", "granian", "--host", "127.0.0.1", "--port", str(PORT),
          "--interface", "asgi", "--log-level", "warning"]
    aether = [py, "bench/aether_app.py", "--port", str(PORT)]
    fastapi_uv = uv + ["bench.fastapi_app:app"]
    fastapi_gr = gr + ["--workers", "1", "bench.fastapi_app:app"]
    return {
        "aether":            (aether, "/", []),
        "aether-1w":         (aether + ["--workers", "1"], "/", []),
        "aether-2w":         (aether + ["--workers", "2"], "/", []),
        "aether-4w":         (aether + ["--workers", "4"], "/", []),
        "aether-8w":         (aether + ["--workers", "8"], "/", []),
        "aether-param":      (aether, "/users/42", []),
        "aether-body":       (aether, "/users", POST_JSON),
        "aether-query":      (aether, "/search?q=abc&limit=5", []),
        "uvicorn-raw":       (uv + ["bench.asgi_raw:app"], "/", []),
        "uvicorn-raw-Nw":    (uv + ["--workers", str(workers), "bench.asgi_raw:app"], "/", []),
        "uvicorn-fastapi":   (fastapi_uv, "/", []),
        "uvicorn-fastapi-param": (fastapi_uv, "/users/42", []),
        "uvicorn-fastapi-body":  (fastapi_uv, "/users", POST_JSON),
        "uvicorn-fastapi-query": (fastapi_uv, "/search?q=abc&limit=5", []),
        "granian-raw":       (gr + ["--workers", "1", "bench.asgi_raw:app"], "/", []),
        "granian-raw-Nw":    (gr + ["--workers", str(workers), "bench.asgi_raw:app"], "/", []),
        "granian-fastapi":   (fastapi_gr, "/", []),
        "granian-fastapi-param": (fastapi_gr, "/users/42", []),
        "granian-fastapi-body":  (fastapi_gr, "/users", POST_JSON),
        "granian-fastapi-query": (fastapi_gr, "/search?q=abc&limit=5", []),
    }


def wait_port(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def oha(url: str, seconds: int, conns: int, extra: list[str]) -> dict:
    out = subprocess.run(
        ["oha", "--no-tui", "--output-format", "json", "-z", f"{seconds}s",
         "-c", str(conns), *extra, url],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def run_target(name: str, cmd: list[str], path: str, extra: list[str], args) -> dict | None:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        if not wait_port(PORT, proc):
            err = proc.stderr.read() if proc.stderr else ""
            print(f"  !! {name} failed to start\n{err.strip()[-800:]}", file=sys.stderr)
            return None
        url = f"http://127.0.0.1:{PORT}{path}"
        oha(url, args.warmup, args.conns, extra)
        r = oha(url, args.duration, args.conns, extra)
        s, p = r["summary"], r["latencyPercentiles"]
        return {
            "target": name,
            "path": path,
            "rps": s["requestsPerSec"],
            "p50_ms": p["p50"] * 1000,
            "p99_ms": p["p99"] * 1000,
            "success": s["successRate"],
            "cmd": " ".join(cmd),
        }
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--duration", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--conns", type=int, default=64)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("targets", nargs="*")
    args = ap.parse_args()

    py = str(Path(args.python).absolute())  # keep the venv symlink

    # Recorded because it burned an hour once: a "3.5% regression" turned out to
    # be nothing but leftover load from the previous benchmark run.
    load_before = os.getloadavg()
    if load_before[0] > 2.0:
        print(f"WARNING: 1-minute load average is {load_before[0]:.1f}. Results will be "
              f"depressed and are not comparable with a quiet run.\n", file=sys.stderr)
    info = subprocess.run(
        [py, "-c", "import sys;print(sys.version.split()[0], 'gil' if sys._is_gil_enabled() else 'free-threaded')"],
        capture_output=True, text=True, check=True).stdout.strip()
    print(f"python: {py}\nbuild : {info}")
    print(f"load  : {args.conns} conns x {args.duration}s (warmup {args.warmup}s)")
    print(f"sysload: {load_before[0]:.2f} at start\n")

    cmds = target_cmds(py, args.workers)
    names = args.targets or list(cmds)
    rows = []
    for name in names:
        if name not in cmds:
            print(f"unknown target {name!r}; choose from {', '.join(cmds)}", file=sys.stderr)
            sys.exit(2)
        print(f"-> {name}", flush=True)
        row = run_target(name, *cmds[name], args)
        if row:
            rows.append(row)
            print(f"   {row['rps']:>10.0f} req/s   p50 {row['p50_ms']:.2f} ms   p99 {row['p99_ms']:.2f} ms")

    print(f"\n{'target':<18}{'req/s':>12}{'p50 ms':>10}{'p99 ms':>10}{'ok%':>8}")
    for r in rows:
        print(f"{r['target']:<18}{r['rps']:>12.0f}{r['p50_ms']:>10.2f}{r['p99_ms']:>10.2f}{r['success']*100:>8.1f}")

    RESULTS.mkdir(exist_ok=True)
    tag = "ft" if "free-threaded" in info else "gil"
    out = RESULTS / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({
        "python": info,
        "args": vars(args),
        "loadavg_before": load_before,
        "loadavg_after": os.getloadavg(),
        "rows": rows,
    }, indent=2))
    print(f"\nsaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
