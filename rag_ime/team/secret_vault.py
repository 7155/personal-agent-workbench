"""Small authenticated-encryption boundary for team connection credentials.

The team server stores only the ciphertext returned by this module.  The key
is an operator-owned file and the associated data ties each ciphertext to the
human/project owner, connection id, provider, and secret version.  The vault
does not log or include credential material in its errors.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
import secrets
import stat
from typing import Any

from ..secure_files import parent_descriptor, regular_reader
from .errors import TeamError


try:  # The team extra is optional for the base PAW installation.
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - exercised only without the team extra.
    AESGCM = None  # type: ignore[assignment,misc]
    InvalidTag = ValueError  # type: ignore[misc,assignment]


_KEY_BYTES = 32
_NONCE_BYTES = 12
_MIN_CIPHERTEXT_BYTES = _NONCE_BYTES + 16  # AES-GCM authentication tag.
_MAX_SECRET_KEYS = 64
_MAX_SECRET_BYTES = 64 * 1024
_MAX_SECRET_VALUE_CHARS = 16 * 1024
_MAX_AAD_COMPONENT_CHARS = 256
_SYSTEM_PATH_SYMLINKS = frozenset({Path("/tmp"), Path("/var")})


def _vault_error() -> TeamError:
    return TeamError(503, "connection_vault_unavailable", "connection credentials are unavailable")


def _invalid_secret() -> TeamError:
    return TeamError(409, "connection_secret_invalid", "connection credentials are invalid")


def _component(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_AAD_COMPONENT_CHARS:
        raise _invalid_secret()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _invalid_secret()
    return value


def _aad(owner_id: object, connection_id: object, provider: object, version: object) -> bytes:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _invalid_secret()
    parts = (
        _component(owner_id),
        _component(connection_id),
        _component(provider),
        str(version),
    )
    return ("paw-team-connection-v1\x00" + "\x00".join(parts)).encode("utf-8")


def _validate_secret(credentials: object) -> tuple[dict[str, Any], bytes]:
    if not isinstance(credentials, Mapping) or len(credentials) > _MAX_SECRET_KEYS:
        raise TeamError(400, "invalid_input", "credentials have an invalid shape")
    copied: dict[str, Any] = {}
    for key, value in credentials.items():
        if not isinstance(key, str) or not key or len(key) > 128:
            raise TeamError(400, "invalid_input", "credentials have an invalid shape")
        if any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise TeamError(400, "invalid_input", "credentials have an invalid shape")
        if isinstance(value, str) and len(value) > _MAX_SECRET_VALUE_CHARS:
            raise TeamError(400, "invalid_input", "credentials are too large")
        copied[key] = value
    try:
        encoded = json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TeamError(400, "invalid_input", "credentials have an invalid shape") from exc
    if len(encoded) > _MAX_SECRET_BYTES:
        raise TeamError(400, "invalid_input", "credentials are too large")
    return copied, encoded


class TeamSecretVault:
    """Encrypt and decrypt bounded JSON credential bundles with AES-GCM."""

    def __init__(self, key_path: str | Path, *, create_if_missing: bool = True) -> None:
        if AESGCM is None:
            raise _vault_error()
        self.key_path = Path(key_path)
        self._secure_key_path = self._secure_path(self.key_path)
        self._key = self._load_or_create_key(create_if_missing=create_if_missing)

    @staticmethod
    def _secure_path(path: Path) -> Path:
        """Resolve only the platform's root temp aliases, rejecting others.

        macOS commonly exposes ``/tmp`` and ``/var`` as privileged aliases to
        ``/private``.  They are safe to canonicalize before the descriptor
        walk; an application-owned symlink anywhere below them remains a hard
        error and can never be followed by the vault.
        """

        absolute = path.absolute()
        if ".." in absolute.parts or not absolute.is_absolute():
            raise _vault_error()
        current = Path(absolute.anchor)
        for index, part in enumerate(absolute.parts[1:]):
            candidate = current / part
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                current = candidate
                continue
            if stat.S_ISLNK(info.st_mode):
                if index == len(absolute.parts[1:]) - 1 or candidate not in _SYSTEM_PATH_SYMLINKS:
                    raise _vault_error()
                target = candidate.resolve(strict=True)
                if not target.is_absolute():
                    raise _vault_error()
                current = target
            else:
                current = candidate
        return current

    @staticmethod
    def _validate_key_file(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("vault key is not a regular file")
        if info.st_mode & (0o077 | stat.S_IXUSR) or not info.st_mode & stat.S_IRUSR:
            raise OSError("vault key permissions are too broad")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise OSError("vault key owner is unexpected")

    def _read_key(self) -> bytes:
        # Both the parent walk and final open are descriptor-relative and
        # O_NOFOLLOW.  The validation is against the already-open descriptor,
        # so a concurrent rename cannot turn a regular key into a symlink.
        with regular_reader(self._secure_key_path) as source:
            self._validate_key_file(source.fileno())
            key = source.read(_KEY_BYTES + 1)
        if len(key) != _KEY_BYTES:
            raise OSError("vault key has an invalid size")
        return key

    def _create_key(self) -> bytes | None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with parent_descriptor(self._secure_key_path, create=True) as (parent, name):
            parent_info = os.fstat(parent)
            if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_mode & 0o077:
                raise OSError("vault key parent permissions are too broad")
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=parent)
            except FileExistsError:
                # Another initializer won the exclusive create.  Let the
                # caller reopen and validate that winner's descriptor.
                return None
            try:
                os.fchmod(descriptor, 0o600)
                key = secrets.token_bytes(_KEY_BYTES)
                with os.fdopen(descriptor, "wb", closefd=True) as target:
                    descriptor = -1
                    target.write(key)
                    target.flush()
                    os.fsync(target.fileno())
                os.fsync(parent)
                return key
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def _load_or_create_key(self, *, create_if_missing: bool) -> bytes:
        try:
            return self._read_key()
        except FileNotFoundError:
            if not create_if_missing:
                raise _vault_error()
            try:
                created = self._create_key()
            except (OSError, ValueError):
                raise _vault_error() from None
            if created is not None:
                return created
            try:
                return self._read_key()
            except (OSError, ValueError):
                raise _vault_error() from None
        except (OSError, ValueError):
            raise _vault_error() from None

    @staticmethod
    def validate(credentials: object) -> dict[str, Any]:
        """Validate a credential bundle without persisting or exposing it."""

        return _validate_secret(credentials)[0]

    def seal(
        self,
        credentials: object,
        *,
        owner_id: object,
        connection_id: object,
        provider: object,
        version: int,
    ) -> bytes:
        _validated, encoded = _validate_secret(credentials)
        associated_data = _aad(owner_id, connection_id, provider, version)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        try:
            assert AESGCM is not None
            encrypted = AESGCM(self._key).encrypt(nonce, encoded, associated_data)
        except (TypeError, ValueError):
            raise _vault_error() from None
        return nonce + encrypted

    def open(
        self,
        sealed: object,
        *,
        owner_id: object,
        connection_id: object,
        provider: object,
        version: int,
    ) -> dict[str, Any]:
        if not isinstance(sealed, (bytes, bytearray)):
            raise _invalid_secret()
        encrypted = bytes(sealed)
        if len(encrypted) < _MIN_CIPHERTEXT_BYTES or len(encrypted) > _MAX_SECRET_BYTES + _NONCE_BYTES + 16:
            raise _invalid_secret()
        associated_data = _aad(owner_id, connection_id, provider, version)
        try:
            assert AESGCM is not None
            decoded = AESGCM(self._key).decrypt(
                encrypted[:_NONCE_BYTES], encrypted[_NONCE_BYTES:], associated_data
            )
            credentials = json.loads(decoded.decode("utf-8"))
        except (InvalidTag, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise _invalid_secret() from None
        if not isinstance(credentials, Mapping):
            raise _invalid_secret()
        try:
            validated, _encoded = _validate_secret(credentials)
        except TeamError:
            raise _invalid_secret() from None
        return validated


# A short compatibility name is useful to the manager while keeping the
# descriptive class name available to direct callers and tests.
SecretVault = TeamSecretVault


__all__ = ["SecretVault", "TeamSecretVault"]
