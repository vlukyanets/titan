from __future__ import annotations

import pytest
from httpx import AsyncClient

from titan import __version__


async def test_liveness_needs_no_database(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_readiness_reports_problem_when_database_is_down(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 503
    assert "nobody" not in response.text  # credentials never leak into errors


async def test_unknown_route_is_problem_json(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"] == "Not Found"


@pytest.mark.db
async def test_readiness_reports_revision(db_client: AsyncClient) -> None:
    response = await db_client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
