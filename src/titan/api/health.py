"""Liveness and readiness probes. Unauthenticated so container health checks can use them."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan import __version__
from titan.api.problems import PROBLEM_JSON, problem

router = APIRouter(prefix="/health", tags=["health"])


class Liveness(BaseModel):
    status: Literal["ok"]
    version: str


class Readiness(BaseModel):
    status: Literal["ready"]
    database_revision: str | None


@router.get("", summary="Process is up")
async def liveness() -> Liveness:
    return Liveness(status="ok", version=__version__)


@router.get(
    "/ready",
    summary="Database is reachable",
    responses={503: {"content": {PROBLEM_JSON: {}}, "description": "Database unavailable"}},
    response_model=Readiness,
)
async def readiness(request: Request) -> Readiness | JSONResponse:
    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.connect() as conn:
            revision = await _revision(conn)
    except Exception:
        return problem(503, "Service Unavailable", "database is not reachable")
    return Readiness(status="ready", database_revision=revision)


async def _revision(conn: AsyncConnection) -> str | None:
    exists = await conn.scalar(text("select to_regclass('alembic_version') is not null"))
    if not exists:
        return None
    value = await conn.scalar(text("select version_num from alembic_version"))
    return str(value) if value is not None else None
