from __future__ import annotations

import pytest

from titan.domains.accounts import credentials
from titan.domains.accounts.errors import InvalidPasswordError, InvalidUsernameError


@pytest.mark.parametrize(("raw", "normalized"), [("Alice", "alice"), (" bob.k_1-x ", "bob.k_1-x")])
def test_normalizes_valid_usernames(raw: str, normalized: str) -> None:
    assert credentials.normalize_username(raw) == normalized


@pytest.mark.parametrize("raw", ["ab", "a" * 33, "anna maria", "анна", "x@y"])
def test_rejects_invalid_usernames(raw: str) -> None:
    with pytest.raises(InvalidUsernameError):
        credentials.normalize_username(raw)


@pytest.mark.parametrize("password", ["short", "x" * 1025])
def test_rejects_bad_password_lengths(password: str) -> None:
    with pytest.raises(InvalidPasswordError):
        credentials.check_password(password)


async def test_password_round_trip() -> None:
    hashed = await credentials.hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert await credentials.verify_password(hashed, "correct horse battery")
    assert not await credentials.verify_password(hashed, "wrong horse battery")


async def test_missing_hash_never_verifies() -> None:
    assert not await credentials.verify_password(None, "anything at all")


def test_tokens_are_prefixed_random_and_hashed() -> None:
    first, second = credentials.new_device_token(), credentials.new_device_token()
    assert first.startswith("tt_")
    assert first != second
    assert len(first) >= 40
    digest = credentials.token_digest(first)
    assert len(digest) == 64
    assert digest != first
    assert digest == credentials.token_digest(first)
