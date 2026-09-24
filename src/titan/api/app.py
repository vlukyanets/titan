"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from titan import __version__
from titan.api import accounts, health, problems
from titan.settings import Settings
from titan.storage.db import create_engine, session_factory

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="TITAN API",
        version=__version__,
        summary="Self-hosted AI assistant, planner and tracker",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = session_factory(engine)
    problems.install(app)
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(accounts.router, prefix=API_PREFIX)
    return app
