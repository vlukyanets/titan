#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker compose up -d n1
for _ in $(seq 120); do
  docker exec yugabyte-n1 bin/ysqlsh -h n1 -c "select 1" >/dev/null 2>&1 && break
  sleep 2
done
docker compose up -d n2
sleep 20
docker compose up -d n3
for _ in $(seq 120); do
  docker exec yugabyte-n3 bin/ysqlsh -h n3 -c "select 1" >/dev/null 2>&1 && break
  sleep 2
done
sleep 10
docker exec yugabyte-n1 bin/yb-admin --master_addresses n1:7100,n2:7100,n3:7100 get_universe_config \
  | grep -o '"numReplicas":[0-9]*' || true
docker exec yugabyte-n1 bin/ysqlsh -h n1 -c "select version();" -c "select host, node_type from yb_servers();"
