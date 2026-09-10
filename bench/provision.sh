#!/usr/bin/env bash
#
# Take a bare Linux box to a state where `make bench-all` produces trustworthy
# numbers. Written for Ubuntu 24.04 on a dedicated-CPU cloud instance, which is
# the only shape of host these benchmarks are meant to run on.
#
#     curl -fsSL <raw url>/bench/provision.sh | bash
#     # or, on a box that already has the repo:
#     bench/provision.sh
#
# Everything here is idempotent: run it again after a reboot and it will only
# redo the parts that did not survive.
#
# It does four things beyond installing packages, and those four are the reason
# this is a script rather than a paragraph in a README:
#
#   * pins the CPU governor to `performance`, because a cloud image usually
#     ships `powersave` and that caps sustained frequency;
#   * raises the file-descriptor limit, because the default 1024 makes a
#     streaming benchmark fail as a timeout rather than as an error;
#   * disables unattended-upgrades, which otherwise wakes up mid-run and
#     competes for the cores it is measuring;
#   * finishes by printing the machine fingerprint and preflight, so an unfit
#     host is obvious before an hour is spent on it.
set -euo pipefail

REPO="${AETHER_REPO:-https://github.com/ishangavidusha/aether.git}"
DIR="${AETHER_DIR:-$HOME/aether}"
FD_LIMIT=65536

# A cloud instance usually logs you in as root, where sudo may not be installed
# at all. Calling it unconditionally fails on exactly the hosts this targets.
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

if [ "$(uname -s)" != "Linux" ]; then
    echo "This provisions a Linux benchmark host. On macOS the environment is already set up by 'make venvs'." >&2
    exit 1
fi

say "packages"
export DEBIAN_FRONTEND=noninteractive
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq build-essential pkg-config libssl-dev curl git ca-certificates

say "quieting the machine"
# A background apt run mid-benchmark is indistinguishable from a regression.
$SUDO systemctl disable --now unattended-upgrades 2>/dev/null || true
$SUDO systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

# Governor. Not every kernel exposes one — a VM often does not, and that is
# fine; what is not fine is silently measuring at a capped frequency.
if compgen -G "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor" >/dev/null; then
    echo performance | $SUDO tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor >/dev/null
    echo "governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
else
    echo "governor: not exposed by this kernel; nothing to set"
fi

# Descriptor limit, for this shell and for every future login.
if ! grep -q "aether benchmark" /etc/security/limits.conf 2>/dev/null; then
    printf '# aether benchmark\n* soft nofile %s\n* hard nofile %s\n' "$FD_LIMIT" "$FD_LIMIT" \
        | $SUDO tee -a /etc/security/limits.conf >/dev/null
fi
ulimit -n "$FD_LIMIT" 2>/dev/null || echo "could not raise nofile in this shell; log out and back in"

say "rust"
if ! command -v cargo >/dev/null; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path
fi
# shellcheck disable=SC1091
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
rustc --version

say "uv"
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

say "oha"
# The load generator. Cargo builds it from source, which takes a few minutes
# once and then never again.
command -v oha >/dev/null || cargo install oha --locked

say "aether"
if [ ! -d "$DIR/.git" ]; then
    git clone "$REPO" "$DIR"
fi
cd "$DIR"
git pull --ff-only 2>/dev/null || true

say "environments"
make venvs
make build

say "checking the build works before measuring it"
# A sanity check that the extension imports and dispatch works, not a full
# validation: durable topics print SKIP without a Redis, and this host has none.
make verify

say "machine"
.venv/bin/python bench/machine.py

cat <<'DONE'

Ready. The full battery, both interpreter builds:

    make bench-all

Add PROFILE=quick for a fast sanity pass first. Results land in bench/results/
and are collected into one tarball at the end; copy that back rather than the
directory, so the machine fingerprint travels with the numbers.
DONE
