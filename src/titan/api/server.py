"""titan-api entry point."""

from __future__ import annotations

import asyncio

import uvicorn

from titan.api.app import create_app
from titan.settings import Settings
from titan.storage.db import create_engine
from titan.storage.revision import check_revision


async def _check_schema(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        await check_revision(engine)
    finally:
        await engine.dispose()


def main() -> None:
    settings = Settings()
    settings.check_bind()
    # Refuse to serve on a schema older than the code (database-migrations.md).
    asyncio.run(_check_schema(settings))
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=False,
    )
