#!/usr/bin/env bash
# Initialises a standalone Postgres 18 node with the Spock settings from pgEdge's examples.
set -euo pipefail
if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  initdb -D "$PGDATA" --encoding=UTF8 --locale=C.UTF-8 --auth=scram-sha-256 --username=postgres --pwfile=<(echo spike-password) >/dev/null
  cat >> "$PGDATA/postgresql.conf" <<CONF
listen_addresses = '*'
shared_preload_libraries = 'spock'
wal_level = logical
track_commit_timestamp = on
max_worker_processes = 16
max_replication_slots = 16
max_wal_senders = 16
spock.enable_ddl_replication = on
spock.include_ddl_repset = on
spock.allow_ddl_from_functions = on
spock.conflict_resolution = last_update_wins
# PostgreSQL 18.6 only lets listed libraries serve as logical decoding output plugins.
output_plugin_libraries = 'pgoutput, spock_output'
spock.save_resolutions = on
CONF
  echo "host all all 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"
  echo "host replication all 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"
fi
exec postgres -D "$PGDATA"
