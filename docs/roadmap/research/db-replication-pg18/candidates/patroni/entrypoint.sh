#!/usr/bin/env bash
# One node = one etcd member + one Patroni-managed Postgres 18. NODE is n1, n2 or n3.
set -euo pipefail
if [[ $(id -u) == 0 ]]; then
  # The shared archive volume is created root-owned; Postgres refuses to run as root.
  chown postgres /archive
  chmod 700 /var/lib/postgresql/data
  exec runuser -u postgres -- env NODE="$NODE" NOFAILOVER="${NOFAILOVER:-}" NOSYNC="${NOSYNC:-}" bash "$0"
fi
mkdir -p /var/lib/postgresql/etcd
etcd --name "$NODE" --data-dir /var/lib/postgresql/etcd \
  --listen-client-urls http://0.0.0.0:2379 --advertise-client-urls "http://$NODE:2379" \
  --listen-peer-urls http://0.0.0.0:2380 --initial-advertise-peer-urls "http://$NODE:2380" \
  --initial-cluster n1=http://n1:2380,n2=http://n2:2380,n3=http://n3:2380 \
  --initial-cluster-state new --initial-cluster-token titan-spike \
  > /var/lib/postgresql/etcd.log 2>&1 &
sed "s/__NODE__/$NODE/g; s/__NOFAILOVER__/${NOFAILOVER:-false}/; s/__NOSYNC__/${NOSYNC:-false}/" \
  /patroni.yml.tmpl > /tmp/patroni.yml
exec patroni /tmp/patroni.yml
