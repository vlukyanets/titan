#!/usr/bin/env bash
# Runs the cr-sqlite spike end to end: build the node image, start a clean
# 3-node cluster, run the scenarios, tear everything down.
#   KEEP_IMAGE=1 ./run.sh      keep the built image afterwards
#   ./run.sh s1 s2             run only some scenarios (after s5 created the schema)
set -euo pipefail
cd "$(dirname "$0")"
UV=${UV:-/root/.local/bin/uv}
export UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT:-/tmp/spike-venv-crsqlite}
P="docker compose -p spike-crsqlite"

docker build --network host --build-context ccr=/root/.ccr \
  --build-arg HTTPS_PROXY="${HTTPS_PROXY:-}" \
  -f node/Dockerfile -t spike-crsqlite-node:latest .

$P --profile restore down -v --remove-orphans
$P up -d
trap '$P --profile restore down -v --remove-orphans; [ "${KEEP_IMAGE:-0}" = 1 ] || docker image rm spike-crsqlite-node:latest' EXIT

if [ $# -gt 0 ] && [[ " $* " != *" s5 "* ]]; then
  set -- s5 "$@"
fi
"$UV" run --python 3.11 python harness.py "$@" 2>&1 | tee harness.log
