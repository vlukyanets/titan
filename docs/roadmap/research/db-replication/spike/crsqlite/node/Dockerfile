# syntax=docker/dockerfile:1
# Node image: Python + SQLite + cr-sqlite (GitHub release) + sqlite-vec (PyPI).
# Build (from run.sh):
#   docker build --network host --build-context ccr=/root/.ccr \
#     --build-arg HTTPS_PROXY=$HTTPS_PROXY -f node/Dockerfile -t spike-crsqlite-node .
FROM python:3.11-slim

ARG HTTPS_PROXY
ARG https_proxy=${HTTPS_PROXY}
ARG CRSQLITE_VERSION=v0.16.3
ARG CRSQLITE_SHA256=8f6fd31a2be2ba8c3101aad067a504a2e63c8e9b51cc4ace786009c02e7ecbae
ARG SQLITE_VEC_VERSION=0.1.10a4

# The proxy CA is only mounted for the build; it does not end up in the image.
RUN --mount=type=bind,from=ccr,source=ca-bundle.crt,target=/tmp/ccr-ca.crt \
    set -eux; \
    export SSL_CERT_FILE=/tmp/ccr-ca.crt PIP_CERT=/tmp/ccr-ca.crt; \
    pip install --no-cache-dir "sqlite-vec==${SQLITE_VEC_VERSION}" "alembic==1.16.5" "SQLAlchemy==2.0.43"; \
    python - <<PY
import hashlib, io, urllib.request, zipfile
url = "https://github.com/vlcn-io/cr-sqlite/releases/download/${CRSQLITE_VERSION}/crsqlite-linux-x86_64.zip"
data = urllib.request.urlopen(url, timeout=120).read()
assert hashlib.sha256(data).hexdigest() == "${CRSQLITE_SHA256}", "checksum mismatch"
zipfile.ZipFile(io.BytesIO(data)).extract("crsqlite.so", "/opt/ext")
PY
RUN python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); c.load_extension('/opt/ext/crsqlite'); import sqlite_vec; sqlite_vec.load(c); print(sqlite3.sqlite_version, c.execute('select vec_version()').fetchone()); c.execute('select crsql_finalize()')"

ENV CRSQLITE_PATH=/opt/ext/crsqlite TITAN_DB_PATH=/data/titan.db PYTHONUNBUFFERED=1
WORKDIR /app
COPY node/node.py node/entrypoint.sh node/probe_batch_alter.py /app/
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
RUN mkdir -p /data && chmod +x /app/entrypoint.sh
VOLUME /data
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
