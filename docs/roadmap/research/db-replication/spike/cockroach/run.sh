#!/usr/bin/env bash
# Runs every scenario from a clean cluster and tears it down at the end.
# KEEP=1 ./run.sh leaves the cluster running.
set -euo pipefail
cd "$(dirname "$0")"
UV="${UV:-$(command -v uv || echo /root/.local/bin/uv)}"
mkdir -p out
"$UV" sync --python 3.11 --quiet
"$UV" run --python 3.11 python harness.py "$@" 2>&1 | tee out/run.log
