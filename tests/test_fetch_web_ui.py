"""The image's pinned Web UI: hash check and safe extraction (scripts/fetch_web_ui.py)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_web_ui", ROOT / "scripts/fetch_web_ui.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


fetch_web_ui = _load()


def archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def pinned(tmp_path: Path, data: bytes, version: str = "0.1.0") -> tuple[Path, Path]:
    """Writes an archive and a pin for it; returns (pin file, archive file)."""
    archive_file = tmp_path / f"titan-web-{version}.tar.gz"
    archive_file.write_bytes(data)
    pin_file = tmp_path / "web-ui.json"
    pin_file.write_text(
        json.dumps({"version": version, "sha256": hashlib.sha256(data).hexdigest()})
    )
    return pin_file, archive_file


GOOD = {"./index.html": b"<!doctype html>", "./assets/index-abc.js": b"1"}


def test_a_matching_archive_is_extracted(tmp_path: Path) -> None:
    pin_file, archive_file = pinned(tmp_path, archive(GOOD))
    dest = tmp_path / "web"

    assert fetch_web_ui.main([str(pin_file), str(dest), "--archive", str(archive_file)]) == 0
    assert (dest / "index.html").read_bytes() == b"<!doctype html>"
    assert (dest / "assets" / "index-abc.js").read_bytes() == b"1"


def test_an_archive_with_another_hash_is_refused_before_extraction(tmp_path: Path) -> None:
    pin_file, archive_file = pinned(tmp_path, archive(GOOD))
    archive_file.write_bytes(archive({**GOOD, "./evil.js": b"2"}))
    dest = tmp_path / "web"

    with pytest.raises(fetch_web_ui.FetchError, match="pins"):
        fetch_web_ui.fetch(pin_file, dest, archive_file)
    assert not dest.exists()


def test_a_member_that_climbs_out_of_the_directory_is_refused(tmp_path: Path) -> None:
    pin_file, archive_file = pinned(tmp_path, archive({**GOOD, "../escape.html": b"x"}))
    dest = tmp_path / "web"

    assert fetch_web_ui.main([str(pin_file), str(dest), "--archive", str(archive_file)]) == 1
    assert not (tmp_path / "escape.html").exists()


def test_an_absolute_member_stays_inside_the_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside.html"
    pin_file, archive_file = pinned(tmp_path, archive({**GOOD, str(outside): b"x"}))
    dest = tmp_path / "web"

    fetch_web_ui.fetch(pin_file, dest, archive_file)

    assert not outside.exists()
    assert (dest / str(outside).lstrip("/")).read_bytes() == b"x"


def test_an_archive_without_index_html_is_refused(tmp_path: Path) -> None:
    pin_file, archive_file = pinned(tmp_path, archive({"./assets/index-abc.js": b"1"}))

    with pytest.raises(fetch_web_ui.FetchError, match=r"no index\.html"):
        fetch_web_ui.fetch(pin_file, tmp_path / "web", archive_file)


def test_a_non_empty_destination_is_refused(tmp_path: Path) -> None:
    pin_file, archive_file = pinned(tmp_path, archive(GOOD))
    dest = tmp_path / "web"
    dest.mkdir()
    (dest / "old.html").write_text("old")

    with pytest.raises(fetch_web_ui.FetchError, match="not empty"):
        fetch_web_ui.fetch(pin_file, dest, archive_file)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"version": "0.1.0"}),
        json.dumps({"version": "latest", "sha256": "0" * 64}),
        json.dumps({"version": "0.1.0", "sha256": "ABC"}),
    ],
)
def test_a_malformed_pin_is_refused(tmp_path: Path, content: str) -> None:
    pin_file = tmp_path / "web-ui.json"
    pin_file.write_text(content)

    with pytest.raises(fetch_web_ui.FetchError):
        fetch_web_ui.read_pin(pin_file)


def test_the_release_url_is_the_titan_web_github_release() -> None:
    pin = fetch_web_ui.Pin("0.1.0", "0" * 64)

    assert pin.url == (
        "https://github.com/vlukyanets/titan-web/releases/download/v0.1.0/titan-web-0.1.0.tar.gz"
    )


def test_only_https_is_downloaded() -> None:
    with pytest.raises(fetch_web_ui.FetchError, match="HTTPS"):
        fetch_web_ui.download("http://example.invalid/titan-web.tar.gz")


def test_the_committed_pin_is_valid() -> None:
    pin = fetch_web_ui.read_pin(ROOT / "web-ui.json")

    assert pin.url.endswith(f"/v{pin.version}/titan-web-{pin.version}.tar.gz")
