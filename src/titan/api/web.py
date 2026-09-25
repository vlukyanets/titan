"""The Web UI's static files, served next to the API (titan-web ADR 0002).

`/assets/…` holds content-hashed files that never change, so browsers keep
them for a year. Every other path outside `/api` is the UI: an existing file
of the build, or `index.html` so the UI's router can handle deep links. Both
are revalidated on every load, so a node upgrade reaches open browsers.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from titan.api.security import is_api_path

ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
PAGE_CACHE_CONTROL = "no-cache"
_READ_METHODS = ("GET", "HEAD")
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


class _BuildFileResponse(FileResponse):
    """A file of the build with a content-based ETag and no Last-Modified.

    Release archives fix every timestamp, and the default ETag comes from the
    time and size alone, so two releases of index.html of equal length would
    share one: an If-Range request could then mix their bytes.
    """

    def __init__(self, path: Path, etag: str, cache_control: str) -> None:
        super().__init__(path, headers={"Cache-Control": cache_control, "ETag": etag})

    def set_stat_headers(self, stat_result: os.stat_result) -> None:
        self.headers.setdefault("content-length", str(stat_result.st_size))


class WebUI:
    """A build of titan-web on disk: `index.html` plus its assets."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.index = self.root / "index.html"
        if not self.index.is_file():
            raise ValueError(
                f"TITAN_WEB_UI_DIR={root} has no index.html; point it at a titan-web build "
                "or leave it unset to serve only the API"
            )
        # Keyed by size and modification time too, so a development build that
        # is rebuilt in place gets fresh ETags.
        self._etags: dict[tuple[Path, int, int], str] = {}

    def response(self, file: Path, cache_control: str) -> FileResponse:
        stat = file.stat()
        key = (file, stat.st_size, stat.st_mtime_ns)
        etag = self._etags.get(key)
        if etag is None:
            digest = hashlib.sha256(file.read_bytes()).hexdigest()[:32]
            etag = self._etags[key] = f'"{digest}"'
        return _BuildFileResponse(file, etag, cache_control)

    def find(self, relative: str) -> Path | None:
        """The file at a URL path inside the build, or None.

        Hidden names, parent references and anything that resolves outside the
        build (absolute paths, symbolic links) count as not found.
        """
        parts = PurePosixPath(relative).parts
        if not parts or any(part.startswith((".", "/")) for part in parts):
            return None
        try:
            candidate = self.root.joinpath(*parts).resolve()
            if candidate.is_relative_to(self.root) and candidate.is_file():
                return candidate
        except (OSError, ValueError):
            pass
        return None


def router(ui: WebUI) -> APIRouter:
    """Routes for the UI. Include after every API router: the last one catches all."""
    routes = APIRouter(include_in_schema=False)

    @routes.api_route("/assets/{path:path}", methods=list(_READ_METHODS))
    async def asset(path: str) -> Response:
        # A missing asset is an error, never index.html: a stale page must not
        # load HTML as a script.
        file = ui.find(f"assets/{path}")
        if file is None:
            raise HTTPException(status_code=404, detail="no such file in the Web UI")
        return ui.response(file, ASSET_CACHE_CONTROL)

    # Every method, so an unknown API path keeps answering 404 rather than 405.
    @routes.api_route("/{path:path}", methods=_ALL_METHODS)
    async def page(path: str, request: Request) -> Response:
        if is_api_path(f"/{path}"):
            raise HTTPException(status_code=404, detail="Not Found")
        if request.method not in _READ_METHODS:
            raise HTTPException(
                status_code=405, detail="Method Not Allowed", headers={"Allow": "GET, HEAD"}
            )
        return ui.response(ui.find(path) or ui.index, PAGE_CACHE_CONTROL)

    return routes
