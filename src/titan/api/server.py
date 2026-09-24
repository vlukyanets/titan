"""titan-api entry point."""

from __future__ import annotations

import uvicorn

from titan.api.app import create_app
from titan.settings import Settings


def main() -> None:
    settings = Settings()
    settings.check_bind()
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=False,
    )
