# PostgreSQL 17 (patched for Spock) + pgEdge Spock + pgvector, built from source.
#
# Why from source: the current official image (ghcr.io/pgedge/pgedge-postgres,
# built from https://github.com/pgEdge/postgres-images) and the pgEdge package
# repo (dnf.pgedge.com) are not reachable from the spike host (egress proxy
# answers 403). The older Docker Hub image pgedge/pgedge is deprecated
# (https://github.com/pgEdge/pgedge-docker says "no longer actively maintained")
# and ships Spock 5.0.1. So we follow the "Building the Extension from Source"
# procedure in https://github.com/pgEdge/spock/blob/v5.0.11/docs/install_spock.md:
# patch Postgres with spock/patches/<major>/*.diff, build it, then build spock.
#
# Versions are pinned to what pgEdge ships in its own images
# (postgres-images/packagelists/amd64/pg17.11-spock5.0.11-standard.txt),
# except pgvector 0.8.6 (latest tag) instead of 0.8.5.
#
# Build (see run.sh):
#   docker build --network host --build-context ccr=/root/.ccr \
#     --build-arg HTTPS_PROXY=$HTTPS_PROXY --build-arg https_proxy=$HTTPS_PROXY \
#     -t titan-spike-pgedge:local .

ARG PG_TAG=REL_17_11
ARG SPOCK_TAG=v5.0.11
ARG PGVECTOR_TAG=v0.8.6

FROM ubuntu:24.04 AS build
ARG PG_TAG
ARG SPOCK_TAG
ARG PGVECTOR_TAG
ARG HTTPS_PROXY
ARG https_proxy
ENV DEBIAN_FRONTEND=noninteractive

# Ubuntu archive is plain HTTP (signed by apt keys), reachable directly.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential bison flex pkg-config git ca-certificates patch \
      libreadline-dev zlib1g-dev libjansson-dev libicu-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Trust the egress proxy CA for git over HTTPS (TLS verification stays on).
COPY --from=ccr ca-bundle.crt /usr/local/share/ca-certificates/ccr-ca-bundle.crt
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/certs/ca-certificates.crt

WORKDIR /src
RUN git clone --depth 1 --branch ${PG_TAG} https://github.com/postgres/postgres.git postgres \
 && git clone --depth 1 --branch ${SPOCK_TAG} https://github.com/pgEdge/spock.git spock \
 && git clone --depth 1 --branch ${PGVECTOR_TAG} https://github.com/pgvector/pgvector.git pgvector

# 1. Patch and build PostgreSQL.
RUN cd postgres \
 && for p in ../spock/patches/17/*.diff; do echo "applying $p"; patch -p1 < "$p"; done \
 && ./configure --prefix=/usr/local/pgsql --with-openssl --with-icu --with-readline \
 && make -j3 -s \
 && make -s install \
 && make -C contrib/pg_stat_statements -s install

ENV PATH=/usr/local/pgsql/bin:$PATH

# 2. Spock extension.
RUN cd spock && make -j3 -s && make -s install

# 3. pgvector (OPTFLAGS="" keeps the binary portable, no -march=native).
RUN cd pgvector && make -j3 -s OPTFLAGS="" && make -s install OPTFLAGS=""

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreadline8t64 zlib1g libjansson4 libicu74 libssl3t64 util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r postgres && useradd -r -g postgres -d /var/lib/postgresql -s /bin/bash postgres \
    && mkdir -p /var/lib/postgresql && chown postgres:postgres /var/lib/postgresql
COPY --from=build /usr/local/pgsql /usr/local/pgsql
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/spike.conf /etc/postgresql/spike.conf
ENV PATH=/usr/local/pgsql/bin:$PATH \
    PGDATA=/var/lib/postgresql/data \
    LANG=C.UTF-8
STOPSIGNAL SIGINT
EXPOSE 5432
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
