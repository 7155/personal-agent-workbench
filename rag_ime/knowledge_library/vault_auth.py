"""A machine-local management capability; never exposed to the editor or UI."""

import os
import secrets
from pathlib import Path
from .permissions import secure_directory


def management_token(root: Path) -> str:
    secure_directory(root)
    path = root / "vault-management.secret"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            token = stream.read(256).strip()
        if len(token) < 40:
            raise ValueError("Invalid vault management capability")
        return token
    with os.fdopen(fd, "w") as stream:
        token = secrets.token_urlsafe(48)
        stream.write(token)
        stream.flush()
        os.fsync(stream.fileno())
    return token
