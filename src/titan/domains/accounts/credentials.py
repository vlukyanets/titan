"""Password hashing, device tokens and the rules for usernames and passwords."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from titan.domains.accounts.errors import InvalidPasswordError, InvalidUsernameError

TOKEN_PREFIX = "tt_"  # noqa: S105 - a marker, not a secret
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

_hasher = PasswordHasher()  # Argon2id with argon2-cffi's current recommended parameters


def normalize_username(username: str) -> str:
    value = username.strip().lower()
    if not USERNAME_RE.fullmatch(value):
        raise InvalidUsernameError(
            "usernames are 3-32 characters: lowercase letters, digits, '.', '_' or '-'"
        )
    return value


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidPasswordError(f"passwords need at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise InvalidPasswordError(f"passwords have at most {MAX_PASSWORD_LENGTH} characters")


async def hash_password(password: str) -> str:
    # Argon2 takes tens of milliseconds of CPU; keep it off the event loop.
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    """Check a password. With no hash, verify against a dummy so timing does not tell."""
    target = password_hash or _dummy_hash()
    try:
        ok = await asyncio.to_thread(_hasher.verify, target, password)
    except (VerificationError, InvalidHashError):
        return False
    return ok and password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def new_device_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    # Tokens carry 256 bits of randomness, so a fast hash is enough and allows an
    # indexed lookup; a slow password hash would buy nothing here.
    return hashlib.sha256(token.encode()).hexdigest()
