#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker compose up -d
psql_on() { docker exec -e PGPASSWORD=spike-password "spock-n$1" psql -v ON_ERROR_STOP=1 -qAt -h localhost -U postgres "${@:2}"; }
for n in 1 2 3; do
  for _ in $(seq 60); do psql_on "$n" -c "select 1" >/dev/null 2>&1 && break; sleep 1; done
  psql_on "$n" -c "create role titan login superuser password 'spike-password'" \
               -c "create database titan owner titan"
  # Extensions go on every node before subscribing; Spock does not replicate CREATE EXTENSION reliably.
  psql_on "$n" -d titan -c "create extension spock" -c "create extension vector" \
    -c "select spock.node_create(node_name := 'n$n', dsn := 'host=n$n port=5432 dbname=titan user=titan password=spike-password')" >/dev/null
done
for n in 1 2 3; do
  for p in 1 2 3; do
    [[ $n == "$p" ]] && continue
    psql_on "$n" -d titan -c "select spock.sub_create(
        subscription_name := 'sub_n${n}_n${p}',
        provider_dsn := 'host=n$p port=5432 dbname=titan user=titan password=spike-password',
        replication_sets := array['default', 'default_insert_only', 'ddl_sql'],
        synchronize_structure := false, synchronize_data := false)" >/dev/null
  done
done
sleep 3
for n in 1 2 3; do psql_on "$n" -d titan -c "select subscription_name, status from spock.sub_show_status()"; done
psql_on 1 -d titan -c "select version()" -c "select extversion from pg_extension where extname in ('spock','vector')"
