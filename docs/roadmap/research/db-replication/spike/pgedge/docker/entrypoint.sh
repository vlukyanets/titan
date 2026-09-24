#!/bin/bash
# Minimal entrypoint: initdb on first start, then exec postgres as user postgres.
set -euo pipefail
: "${PGDATA:=/var/lib/postgresql/data}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
: "${POSTGRES_DB:=titan}"
AS_PG="setpriv --reuid=postgres --regid=postgres --init-groups"

mkdir -p "$PGDATA"
chown -R postgres:postgres "$(dirname "$PGDATA")"
chmod 700 "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  pw=$(mktemp); echo "$POSTGRES_PASSWORD" > "$pw"; chown postgres "$pw"
  $AS_PG initdb -D "$PGDATA" -U postgres --pwfile="$pw" -E UTF8 --locale=C.UTF-8 \
      --auth-local=trust --auth-host=scram-sha-256 >/dev/null
  rm -f "$pw"
  cat /etc/postgresql/spike.conf >> "$PGDATA/postgresql.conf"
  if [ "${SPOCK_DISABLED:-0}" = "1" ]; then
    # used by the S7 restore target: plain Postgres + pgvector, no Spock
    sed -i "s/^shared_preload_libraries = 'spock'/shared_preload_libraries = ''/; s/^spock\./#spock./" "$PGDATA/postgresql.conf"
  fi
  {
    echo "host all         all 0.0.0.0/0 scram-sha-256"
    echo "host replication all 0.0.0.0/0 scram-sha-256"
  } >> "$PGDATA/pg_hba.conf"
  $AS_PG pg_ctl -D "$PGDATA" -o "-c listen_addresses=''" -w start >/dev/null
  $AS_PG psql -v ON_ERROR_STOP=1 -U postgres -d postgres -qc "CREATE DATABASE \"$POSTGRES_DB\""
  $AS_PG pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null
  echo "entrypoint: initialised $PGDATA"
fi

exec $AS_PG postgres -D "$PGDATA"
