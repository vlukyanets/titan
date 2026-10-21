FROM python:3.14-slim@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a

COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH=/app/.venv/bin:$PATH

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY . .
RUN uv sync --locked --no-dev

RUN useradd --system --no-create-home titan \
    && install -d -o titan -g titan -m 700 /var/lib/titan
USER titan

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn titan.app:create_app --factory --host 0.0.0.0 --port 8000"]
