import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from titan import keys


def test_node_key_is_created_once_readable_only_by_the_app(tmp_path: Path) -> None:
    path = tmp_path / "node.key"
    encrypted = keys.load_or_create(path).encrypt(b"secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A later start reads the same key, so earlier keys still decrypt.
    assert keys.load_or_create(path).decrypt(encrypted) == b"secret"


def test_a_failed_first_start_leaves_no_broken_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "node.key"

    def crash() -> bytes:
        raise OSError("the disk is full")

    with monkeypatch.context() as patch:
        patch.setattr(Fernet, "generate_key", crash)
        with pytest.raises(OSError):
            keys.load_or_create(path)
    # The next start creates a key instead of failing on an empty file.
    keys.load_or_create(path).encrypt(b"secret")
    assert list(tmp_path.iterdir()) == [path]
