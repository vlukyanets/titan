#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker compose up -d
leader=""
for _ in $(seq 90); do
  leader=$(docker exec patroni-n1 patronictl -c /tmp/patroni.yml list -f json 2>/dev/null \
    | python3 -c "import json,sys; print(next((m['Member'] for m in json.load(sys.stdin) if m['Role']=='Leader'),''))" 2>/dev/null || true)
  [[ -n $leader ]] && break
  sleep 2
done
echo "leader: $leader"
docker exec -e PGPASSWORD=spike-password "patroni-$leader" psql -v ON_ERROR_STOP=1 -qAt -h localhost -U postgres \
  -c "create role titan login superuser password 'spike-password'" -c "create database titan owner titan"
docker exec -e PGPASSWORD=spike-password "patroni-$leader" psql -qAt -h localhost -U postgres -d titan \
  -c "create extension vector" -c "select version()"
sleep 15
docker exec patroni-n1 patronictl -c /tmp/patroni.yml list
