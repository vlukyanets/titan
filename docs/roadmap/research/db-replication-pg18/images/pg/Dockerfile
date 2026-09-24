# PostgreSQL 18 patched for Spock, with Spock, pgvector and Patroni. Built from source
# because the spike host cannot pull ghcr.io/pgedge images. Both the Spock and the
# Patroni candidates run this image, so they share identical Postgres binaries.
ARG BASE=mirror.gcr.io/library/ubuntu:24.04

FROM ${BASE} AS build
ARG PG_TAG=REL_18_6
ARG SPOCK_TAG=v5.0.11
ARG PGVECTOR_TAG=v0.8.6
COPY extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates build-essential bison flex git pkg-config \
      libreadline-dev zlib1g-dev libssl-dev libicu-dev libkrb5-dev libjansson-dev \
    && update-ca-certificates
RUN git clone -q --depth 1 --branch ${PG_TAG} https://github.com/postgres/postgres.git /src/pg \
 && git clone -q --depth 1 --branch ${SPOCK_TAG} https://github.com/pgEdge/spock.git /src/spock \
 && git clone -q --depth 1 --branch ${PGVECTOR_TAG} https://github.com/pgvector/pgvector.git /src/pgvector
RUN cd /src/pg && for p in /src/spock/patches/18/*.diff; do patch -p1 < "$p"; done \
 && ./configure --prefix=/opt/pg --with-openssl --with-icu --without-llvm --without-liburing > /dev/null \
 && make -j"$(nproc)" -s world-bin && make -s install-world-bin
ENV PATH=/opt/pg/bin:$PATH
RUN cd /src/spock && make -s -j"$(nproc)" && make -s install
RUN cd /src/pgvector && make -s OPTFLAGS="" && make -s install

FROM ${BASE}
COPY extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates libreadline8t64 zlib1g libssl3t64 libicu74 libjansson4 libkrb5-3 \
      python3 python3-venv etcd-server etcd-client iproute2 procps curl \
    && update-ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/patroni \
 && /opt/patroni/bin/pip install -q "patroni[etcd3]==4.*" "psycopg[binary]==3.*"
COPY --from=build /opt/pg /opt/pg
RUN useradd -m -u 999 -s /bin/bash postgres && mkdir -p /var/lib/postgresql/data \
 && chown -R postgres /var/lib/postgresql
ENV PATH=/opt/pg/bin:/opt/patroni/bin:$PATH PGDATA=/var/lib/postgresql/data
USER postgres
