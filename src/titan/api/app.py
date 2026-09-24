"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from titan import __version__
from titan.agent.runtime import ChatRuntime
from titan.api import (
    accounts,
    autonomy,
    calendar,
    chat,
    health,
    notifications,
    problems,
    reminders,
    tasks,
    trackers,
    usage,
)
from titan.notify import UnifiedPushSender, new_client
from titan.settings import Settings
from titan.storage.db import create_engine, session_factory

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_engine(settings)
    push_client = new_client(settings.push_timeout_seconds)
    sessions = session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The credential self-check runs at startup, so a broken setup shows in
        # the log right away; chat then answers 503 and the rest keeps working.
        await app.state.chat_runtime.ready()
        yield
        await app.state.chat_runtime.aclose()
        await push_client.aclose()
        await engine.dispose()

    app = FastAPI(
        title="TITAN API",
        version=__version__,
        summary="Self-hosted AI assistant, planner and tracker",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessions
    app.state.pusher = UnifiedPushSender(push_client)
    app.state.chat_runtime = ChatRuntime(settings, sessions, pusher=app.state.pusher)
    problems.install(app)
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(accounts.router, prefix=API_PREFIX)
    app.include_router(notifications.router, prefix=API_PREFIX)
    app.include_router(chat.router, prefix=API_PREFIX)
    app.include_router(autonomy.router, prefix=API_PREFIX)
    app.include_router(usage.router, prefix=API_PREFIX)
    app.include_router(tasks.projects_router, prefix=API_PREFIX)
    app.include_router(tasks.tasks_router, prefix=API_PREFIX)
    app.include_router(reminders.router, prefix=API_PREFIX)
    app.include_router(calendar.router, prefix=API_PREFIX)
    app.include_router(trackers.router, prefix=API_PREFIX)
    return app
