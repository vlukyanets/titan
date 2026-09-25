# The TITAN image: titan-api by default, titan-worker with `command: ["titan-worker"]`.

# The Web UI release pinned in web-ui.json, checked against its SHA-256
# (titan-web ADR 0002). Upgrading the UI is a commit that changes the pin.
FROM python:3.12-slim AS web-ui
WORKDIR /build
COPY web-ui.json scripts/fetch_web_ui.py ./
RUN python fetch_web_ui.py web-ui.json /web

FROM python:3.12-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir "uv>=0.12"
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY alembic.ini ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home-dir /app titan
WORKDIR /app
COPY --from=build --chown=titan /app /app
# Owned by root, so the service user can read the UI but not change it.
COPY --from=web-ui /web /app/web
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 TITAN_WEB_UI_DIR=/app/web
USER titan
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)"]
CMD ["titan-api"]
