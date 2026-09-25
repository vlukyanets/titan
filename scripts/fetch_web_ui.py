"""Fetch the pinned Web UI release into a directory (titan-web ADR 0002).

Reads the pin (a titan-web version and the SHA-256 of its release archive),
downloads the archive from the titan-web GitHub release, or takes a local copy
with --archive, refuses it unless the hash matches, and extracts it. Runs in
the image build before any dependency is installed, so it uses only the
standard library.

    python scripts/fetch_web_ui.py web-ui.json /app/web
    python scripts/fetch_web_ui.py web-ui.json ./web --archive titan-web-0.1.0.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

RELEASE_URL = (
    "https://github.com/vlukyanets/titan-web/releases/download/"
    "v{version}/titan-web-{version}.tar.gz"
)
# A release is a few hundred kilobytes; anything far larger is not one.
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
TIMEOUT_SECONDS = 60

_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class Pin:
    version: str
    sha256: str

    @property
    def url(self) -> str:
        return RELEASE_URL.format(version=self.version)


def read_pin(path: Path) -> Pin:
    try:
        data = json.loads(path.read_text())
        version, sha256 = data["version"], data["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FetchError(f"{path}: expected {{'version': ..., 'sha256': ...}} ({exc})") from exc
    if not isinstance(version, str) or not _VERSION.match(version):
        raise FetchError(f"{path}: version {version!r} is not a release version like 0.1.0")
    if not isinstance(sha256, str) or not _SHA256.match(sha256):
        raise FetchError(f"{path}: sha256 must be 64 lowercase hex digits")
    return Pin(version, sha256)


def download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise FetchError(f"refusing to download over anything but HTTPS: {url}")
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 (HTTPS checked above)
        data: bytes = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise FetchError(f"{url} is larger than {MAX_ARCHIVE_BYTES} bytes")
    return data


def verify(data: bytes, pin: Pin) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != pin.sha256:
        raise FetchError(
            f"titan-web {pin.version}: SHA-256 is {actual}, but web-ui.json pins {pin.sha256}"
        )


def extract(data: bytes, dest: Path) -> None:
    if dest.exists() and any(dest.iterdir()):
        raise FetchError(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        # The data filter refuses absolute paths, links that leave dest and
        # special files, and drops group and other write permissions.
        archive.extractall(dest, filter="data")
    if not (dest / "index.html").is_file():
        raise FetchError("the archive has no index.html at its root")


def fetch(pin_file: Path, dest: Path, archive: Path | None = None) -> Pin:
    pin = read_pin(pin_file)
    data = archive.read_bytes() if archive is not None else download(pin.url)
    verify(data, pin)
    extract(data, dest)
    return pin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pin", type=Path, help="the pin file, web-ui.json")
    parser.add_argument("dest", type=Path, help="directory to extract into; must be empty")
    parser.add_argument("--archive", type=Path, help="use this archive instead of downloading")
    args = parser.parse_args(argv)
    try:
        pin = fetch(args.pin, args.dest, args.archive)
    except (FetchError, OSError, tarfile.TarError) as exc:
        print(f"fetch_web_ui: {exc}", file=sys.stderr)
        return 1
    print(f"titan-web {pin.version} ({pin.sha256}) in {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
