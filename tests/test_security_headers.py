"""ADR 0012: the node's security headers on every response."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from titan.api.security import CONTENT_SECURITY_POLICY, SECURITY_HEADERS

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
    "require-trusted-types-for 'script'"
)


def test_policy_is_the_one_in_adr_0012() -> None:
    assert CONTENT_SECURITY_POLICY == EXPECTED_CSP


@pytest.mark.parametrize(
    ("path", "status"),
    [("/api/v1/health", 200), ("/api/v1/nope", 404), ("/api/v1/me", 401)],
)
async def test_api_responses_carry_the_headers_and_are_not_stored(
    client: AsyncClient, path: str, status: int
) -> None:
    response = await client.get(path)

    assert response.status_code == status
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["Cache-Control"] == "no-store"


async def test_no_cors_headers(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"Origin": "https://evil.example"})

    assert not [name for name in response.headers if name.lower().startswith("access-control-")]


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
async def test_no_interactive_docs_from_a_cdn(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 404
