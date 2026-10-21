"""The HTTP API of the node."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from titan import db


def create_app(database_url: str | None = None) -> FastAPI:
    engine = db.make_engine(database_url or os.environ["TITAN_DATABASE_URL"])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health(response: Response) -> dict[str, str]:
        if await db.ping(engine):
            return {"status": "ok"}
        response.status_code = 503
        return {"status": "database unavailable"}

    return app
