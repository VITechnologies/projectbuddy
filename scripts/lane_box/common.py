"""Shared helpers: digests, private files, and the forbidden-key scan."""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import tempfile
import uuid

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
VM_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
FORBIDDEN_FIELDS = frozenset({
    "activation_code", "credential", "email", "full_name", "fullname",
    "installation_id", "password", "private_key", "serial", "serial_number", "token",
})


class LaneBoxError(Exception):
    """A refusal: the named rule failed and nothing was changed."""


class ChangedError(LaneBoxError):
    """A failure after the command had already changed the guest or the host."""


def sha256_file(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def canonical_uuid(value: object, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as error:
        raise LaneBoxError(f"{what} is not a UUID") from error


def write_private(path: pathlib.Path, content: str) -> None:
    """Write ``content`` atomically with mode 600."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise


def hash_line(path: pathlib.Path) -> str:
    return f"{sha256_file(path)}  {path.name}\n"


def verify_hash_file(hash_path: pathlib.Path, target: pathlib.Path) -> None:
    """Check a ``sha256sum``-style file that names exactly ``target``."""
    if not hash_path.is_file():
        raise LaneBoxError(f"hash file is missing: {hash_path.name}")
    fields = hash_path.read_text(encoding="utf-8").split()
    if len(fields) != 2 or fields[1].lstrip("*") != target.name:
        raise LaneBoxError(f"hash file does not name {target.name}: {hash_path.name}")
    if fields[0] != sha256_file(target):
        raise LaneBoxError(f"hash mismatch: {target.name}")


def require_private_directory(path: pathlib.Path) -> None:
    if not path.is_absolute() or not path.is_dir():
        raise LaneBoxError(f"directory must be an existing absolute path: {path}")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise LaneBoxError(f"directory must have mode 700 or stricter: {path}")


def forbidden_keys(value: object) -> list[str]:
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if str(key).lower() in FORBIDDEN_FIELDS:
                    found.add(str(key).lower())
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found)
