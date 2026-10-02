"""titan-web ADR 0002: the node serves the Web UI next to the API."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import UNREACHABLE_DB
from titan.api.app import create_app
from titan.api.security import CONTENT_SECURITY_POLICY
from titan.api.web import WebUI
from titan.settings import Settings

INDEX = "<!doctype html><title>TITAN</title><script type=module src=/assets/index-abc.js></script>"


@pytest.fixture
def build(tmp_path: Path) -> Path:
    """A stand-in for a titan-web build, with a secret next to it."""
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "favicon.svg").write_text("<svg/>")
    (root / "assets" / "index-abc.js").write_text("console.log('ui')")
    (root / ".env").write_text("SECRET=nope")
    (tmp_path / "secret.txt").write_text("outside the build")
    (root / "assets" / "escape.js").symlink_to(tmp_path / "secret.txt")
    return root


@pytest.fixture
async def ui_client(build: Path) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(database_url=UNREACHABLE_DB, web_ui_dir=build))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()


@pytest.mark.parametrize("path", ["/", "/tasks", "/calendar/2026/09", "/notes/a.b", "/index.html"])
async def test_pages_answer_index_html_for_the_ui_router(ui_client: AsyncClient, path: str) -> None:
    response = await ui_client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


async def test_files_of_the_build_are_served_as_they_are(ui_client: AsyncClient) -> None:
    response = await ui_client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.text == "<svg/>"
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["cache-control"] == "no-cache"


async def test_hashed_assets_are_cached_for_a_year(ui_client: AsyncClient) -> None:
    response = await ui_client.get("/assets/index-abc.js")

    assert response.status_code == 200
    assert response.text == "console.log('ui')"
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_missing_asset_is_not_found_rather_than_the_page(ui_client: AsyncClient) -> None:
    response = await ui_client.get("/assets/index-old.js")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


async def test_etags_follow_the_content_not_the_timestamps(build: Path, tmp_path: Path) -> None:
    # Release archives fix every timestamp; two index.html files of the same
    # length must still get different ETags.
    other = tmp_path / "other"
    (other / "assets").mkdir(parents=True)
    (other / "index.html").write_text(INDEX.replace("abc", "xyz"))
    for root in (build, other):
        os.utime(root / "index.html", (0, 0))
    first, second = WebUI(build), WebUI(other)

    first_tag = first.response(first.index, "no-cache").headers["etag"]
    second_tag = second.response(second.index, "no-cache").headers["etag"]

    assert first_tag != second_tag
    assert first_tag == first.response(first.index, "no-cache").headers["etag"]


async def test_no_last_modified_from_fixed_archive_times(ui_client: AsyncClient) -> None:
    response = await ui_client.get("/tasks")

    assert "last-modified" not in response.headers
    assert response.headers["etag"].startswith('"')


async def test_head_answers_without_a_body(ui_client: AsyncClient) -> None:
    response = await ui_client.head("/tasks")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "path", ["/.env", "/assets/escape.js", "/assets/../../secret.txt", "/%2e%2e/secret.txt"]
)
async def test_nothing_outside_the_build_or_hidden_is_served(
    ui_client: AsyncClient, path: str
) -> None:
    response = await ui_client.get(path)

    assert "SECRET" not in response.text
    assert "outside the build" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api"), ("GET", "/api/v2/tasks"), ("POST", "/api/v1/nope"), ("GET", "/api/v1/nope")],
)
async def test_the_api_keeps_its_own_not_found(
    ui_client: AsyncClient, method: str, path: str
) -> None:
    response = await ui_client.request(method, path)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"


async def test_api_routes_still_answer(ui_client: AsyncClient) -> None:
    response = await ui_client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_writes_to_a_page_are_not_allowed(ui_client: AsyncClient) -> None:
    response = await ui_client.post("/tasks", content=b"x")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


async def test_ui_routes_stay_out_of_the_api_schema(ui_client: AsyncClient) -> None:
    response = await ui_client.get("/openapi.json")

    assert not [path for path in response.json()["paths"] if not path.startswith("/api/")]


async def test_without_a_build_the_node_serves_only_the_api(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_a_directory_without_index_html_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"has no index\.html"):
        create_app(Settings(database_url=UNREACHABLE_DB, web_ui_dir=tmp_path))


def test_find_rejects_paths_that_leave_the_build(build: Path) -> None:
    ui = WebUI(build)

    assert ui.find("favicon.svg") == build.resolve() / "favicon.svg"
    for path in ["", "..", "../secret.txt", "/etc/passwd", "assets/escape.js", ".env", "a\0b"]:
        assert ui.find(path) is None, path
