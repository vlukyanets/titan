#!/usr/bin/env bash
# Runs every scenario of the YugabyteDB spike from a clean cluster, writes
# results.json, then tears the cluster down (containers, networks, volumes).
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/root/.local/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/tmp/spike-venv-yugabyte}"  # keep the venv out of the repo
uv sync --python 3.11 -q
cleanup() { docker compose -p spike-yugabyte --profile restore --profile ddlprobe down -v --remove-orphans; }
trap cleanup EXIT
mkdir -p out
uv run python harness.py all "$@" 2>&1 | tee out/run.log
