#!/usr/bin/env python3
"""The same hello-world server, native and in Docker, measured in one session. (I-021)

    make image-bench && bench/container.py

Docker Desktop on macOS once measured 3.4x slower than native, and the cost was
attributed to the port boundary between macOS and the Linux VM rather than to
containers themselves. That attribution was never tested. This separates the
two, all in one session so the ratios are sound whatever the absolute numbers:

* **native** — server and load generator on the host, the reference.
* **in-network** — server in one container, `oha` in another on the same Docker
  network. Traffic never leaves the VM.
* **published port** — server in a container, `oha` on the host through `-p`.
  The path a developer actually uses, and the one measured before.
* **quota N** — in-network, with the server limited to `--cpus N`, to check that
  worker detection follows the quota and throughput follows the workers.

Absolute container numbers are never compared with native numbers from another
day. What this produces is ratios, and a baseline for containers to be compared
with themselves.
"""
import argparse
import json
import os
import re
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
DOCKER = os.environ.get("DOCKER") or (
    subprocess.run(["sh", "-c", "command -v docker"], capture_output=True, text=True).stdout.strip()
    or str(Path.home() / ".docker/bin/docker")
)
IMAGE = os.environ.get("OXBROOK_BENCH_IMAGE", "oxbrook:bench")
NETWORK = "oxbrook-bench"
SERVER = "oxbrook-bench-server"
NATIVE_PORT = 8781
PUBLISHED_PORT = 8782


def docker(*args, check=True, capture=True):
    return subprocess.run([DOCKER, *args], capture_output=capture, text=True, check=check)


def oha_cmd(url: str, seconds: int, conns: int) -> list[str]:
    return ["oha", "--no-tui", "--output-format", "json", "-z", f"{seconds}s",
            "-c", str(conns), url]


def summarise_oha(raw: str) -> dict:
    r = json.loads(raw)
    return {
        "rps": r["summary"]["requestsPerSec"],
        "p50_ms": r["latencyPercentiles"]["p50"] * 1000,
        "p99_ms": r["latencyPercentiles"]["p99"] * 1000,
        "success": r["summary"]["successRate"],
    }


def wait_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def native(args) -> dict:
    proc = subprocess.Popen(
        [args.python, "bench/oxbrook_app.py", "--port", str(NATIVE_PORT)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not wait_port(NATIVE_PORT):
            raise RuntimeError("native server did not start")
        url = f"http://127.0.0.1:{NATIVE_PORT}/"
        subprocess.run(oha_cmd(url, args.warmup, args.conns), capture_output=True, check=True)
        row = summarise_oha(subprocess.run(oha_cmd(url, args.duration, args.conns),
                                           capture_output=True, text=True, check=True).stdout)
    finally:
        os.killpg(proc.pid, signal.SIGINT)
        out, _ = proc.communicate(timeout=15)
    return {**row, "workers": loops_from(out)}


def loops_from(log: str) -> int | None:
    match = re.search(r"(\d+) worker loop", log or "")
    return int(match.group(1)) if match else None


def containerised(args, cpus: float | None, published: bool) -> dict:
    docker("rm", "-f", SERVER, check=False)
    run = ["run", "-d", "--name", SERVER, "--network", NETWORK]
    if cpus:
        run += ["--cpus", str(cpus)]
    if published:
        run += ["-p", f"127.0.0.1:{PUBLISHED_PORT}:8000"]
    run += [IMAGE, "python", "bench/oxbrook_app.py", "--host", "0.0.0.0", "--port", "8000"]
    docker(*run)
    try:
        deadline = time.time() + 30
        log = ""
        while "listening" not in log:
            if time.time() > deadline:
                raise RuntimeError(f"container server did not start: {log[-400:]}")
            time.sleep(0.2)
            log = docker("logs", SERVER, check=False).stdout + docker("logs", SERVER, check=False).stderr

        if published:
            url = f"http://127.0.0.1:{PUBLISHED_PORT}/"
            if not wait_port(PUBLISHED_PORT):
                raise RuntimeError("published port never opened")
            subprocess.run(oha_cmd(url, args.warmup, args.conns), capture_output=True, check=True)
            raw = subprocess.run(oha_cmd(url, args.duration, args.conns),
                                 capture_output=True, text=True, check=True).stdout
        else:
            url = f"http://{SERVER}:8000/"
            client = ["run", "--rm", "--network", NETWORK, IMAGE]
            docker(*client, *oha_cmd(url, args.warmup, args.conns))
            raw = docker(*client, *oha_cmd(url, args.duration, args.conns)).stdout
        return {**summarise_oha(raw), "workers": loops_from(log)}
    finally:
        docker("rm", "-f", SERVER, check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=str(ROOT / ".venv/bin/python"))
    ap.add_argument("--duration", type=int, default=8)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--conns", type=int, default=64)
    ap.add_argument("--quotas", type=float, nargs="*", default=[4, 2, 1])
    ap.add_argument("--strict", action="store_true",
                    help="refuse to measure when the host fails preflight")
    args = ap.parse_args()
    args.python = str(Path(args.python).absolute())

    session = Session("container", executable=args.python, conns=args.conns, strict=args.strict)
    session.announce()

    if docker("image", "inspect", IMAGE, check=False).returncode != 0:
        print(f"{IMAGE} not found; run `make image-bench` first", file=sys.stderr)
        sys.exit(2)
    info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    inside = json.loads(docker("run", "--rm", IMAGE, "python", "bench/machine.py", "--json").stdout)
    print(f"docker VM: {info.get('NCPU')} cpus, {info.get('MemTotal', 0) / 1024**3:.1f} GiB, "
          f"{info.get('OperatingSystem')}")
    detection = inside.get("worker_detection") or {}
    print(f"inside   : {inside.get('cpu_model')}, detection sees "
          f"performance={detection.get('performance_cores')} "
          f"physical={detection.get('physical_cores')} -> {detection.get('default_workers')} loops\n")

    docker("network", "create", NETWORK, check=False)
    scenarios = [("native", lambda: native(args))]
    scenarios.append(("in-network", lambda: containerised(args, None, False)))
    scenarios.append(("published port", lambda: containerised(args, None, True)))
    for q in args.quotas:
        scenarios.append((f"quota {q:g}", lambda q=q: containerised(args, q, False)))
    # Native again at the end. If the two native runs disagree, the machine
    # changed underneath the session and no ratio in between can be trusted.
    scenarios.append(("native again", lambda: native(args)))

    rows = []
    print(f"{'scenario':<16}{'loops':>6}{'req/s':>10}{'vs native':>11}{'p50 ms':>9}{'p99 ms':>9}")
    try:
        for label, run in scenarios:
            row = {"scenario": label, **run()}
            rows.append(row)
            ratio = row["rps"] / rows[0]["rps"]
            row["vs_native"] = ratio
            print(f"{label:<16}{row['workers'] or '?':>6}{row['rps']:>10.0f}{ratio:>10.2f}x"
                  f"{row['p50_ms']:>9.2f}{row['p99_ms']:>9.2f}", flush=True)
            time.sleep(1.0)
    finally:
        docker("rm", "-f", SERVER, check=False)
        docker("network", "rm", NETWORK, check=False)

    drift = abs(rows[-1]["rps"] - rows[0]["rps"]) / rows[0]["rps"] if len(rows) > 1 else 0
    if drift > 0.05:
        print(f"\n  ! native moved {drift * 100:.0f}% between the first and last run; "
              f"the ratios above are not reliable", file=sys.stderr)

    out = session.finish({
        "native_drift": drift,
        "args": vars(args),
        "docker": {"ncpu": info.get("NCPU"), "mem_bytes": info.get("MemTotal"),
                   "os": info.get("OperatingSystem"), "server_version": info.get("ServerVersion")},
        "inside": inside,
        "rows": rows,
    })
    print(f"\nsaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
