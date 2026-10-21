"""The node key: it encrypts the users' model keys before they are stored."""

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


def load_or_create(path: Path) -> Fernet:
    """Reads the node key, first creating it, readable only by this user."""
    if path.exists():
        return Fernet(path.read_bytes())
    # Written whole to a file of its own, then linked into place, so a
    # start that fails halfway leaves no empty key behind.
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # 0600
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(Fernet.generate_key())
        os.link(temp, path)
    except FileExistsError:
        pass  # another start created it first; use that one
    finally:
        os.unlink(temp)
    return Fernet(path.read_bytes())
