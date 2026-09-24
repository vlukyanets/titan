#!/usr/bin/env bash
# Builds titan-spike/pg:18. The extra CA is only needed behind an intercepting proxy.
set -euo pipefail
cd "$(dirname "$0")"
if [[ -n "${SPIKE_EXTRA_CA:-}" ]]; then cp "$SPIKE_EXTRA_CA" extra-ca.crt; else : > extra-ca.crt; fi
docker build --network host \
  --build-arg http_proxy="${HTTP_PROXY:-}" --build-arg https_proxy="${HTTPS_PROXY:-}" \
  --build-arg HTTP_PROXY="${HTTP_PROXY:-}" --build-arg HTTPS_PROXY="${HTTPS_PROXY:-}" \
  -t titan-spike/pg:18 .
rm -f extra-ca.crt
