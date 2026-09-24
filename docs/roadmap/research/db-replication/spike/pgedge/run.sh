#!/usr/bin/env bash
# Runs every scenario from a clean cluster, then tears the cluster down.
#   ./run.sh            all scenarios (S5, S1, S2, S3, S4, S6, S7)
#   ./run.sh S5 S1      a subset (bootstrap always runs)
# Env: KEEP_CLUSTER=1 skips teardown; REMOVE_IMAGE=1 also removes the built image.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=titan-spike-pgedge:local
UV=$(command -v uv || echo /root/.local/bin/uv)
# keep the virtualenv out of the repository
export UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT:-${TMPDIR:-/tmp}/spike-venv-pgedge}

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE (Postgres 17.11 patched + Spock 5.0.11 + pgvector 0.8.6, ~10 min)"
  CA_DIR=${CA_DIR:-/root/.ccr}   # directory containing ca-bundle.crt (egress proxy CA)
  docker build --network host --build-context ccr="$CA_DIR" \
    --build-arg HTTPS_PROXY="${HTTPS_PROXY:-}" --build-arg https_proxy="${HTTPS_PROXY:-}" \
    -t "$IMAGE" .
fi

teardown() {
  if [ "${KEEP_CLUSTER:-0}" != "1" ]; then
    docker compose -p spike-pgedge --profile restore down -v --remove-orphans
    if [ "${REMOVE_IMAGE:-0}" = "1" ]; then docker image rm "$IMAGE"; fi
  fi
}
trap teardown EXIT

"$UV" run --python 3.11 python harness.py "$@"
