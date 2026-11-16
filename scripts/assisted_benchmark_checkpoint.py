"""Private incomplete checkpoints for bounded local model review."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path


class CheckpointError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("duplicate checkpoint fields")
        result[key] = value
    return result


def private_file(state) -> None:
    if (not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid()
            or stat.S_IMODE(state.st_mode) != 0o600 or state.st_nlink != 1):
        raise CheckpointError("checkpoint file custody is unsafe")


class CheckpointStore:
    def __init__(self, path: Path, binding: str, resume: bool):
        if os.name != "posix":
            raise CheckpointError("private checkpoint custody is unsupported on this platform")
        if not isinstance(binding, str) or not re.fullmatch(r"[a-f0-9]{64}", binding) or type(resume) is not bool:
            raise CheckpointError("checkpoint binding/resume inputs are invalid")
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise CheckpointError("checkpoint requires an absolute non-symlink path")
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.resolve(strict=True) != parent:
            raise CheckpointError("checkpoint directory contains symlinked components")
        state = parent.stat()
        if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
            raise CheckpointError("checkpoint directory must be current-user mode 0700")
        self.binding = binding
        self.state = None
        self.lock = None
        self.digest = None
        self.resume = resume

    def __enter__(self):
        import fcntl

        descriptor = os.open(self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            private_file(os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock = descriptor
            if self.resume:
                self.state = self._read()
            elif self.path.exists() or self.path.is_symlink():
                raise CheckpointError("checkpoint already exists; explicit resume or a new path is required")
            return self
        except BaseException:
            self.lock = None
            os.close(descriptor)
            raise

    def _read(self):
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            private_file(os.fstat(handle.fileno()))
            data = handle.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise CheckpointError("checkpoint exceeds the private record bound")
        try:
            state = json.loads(data, object_pairs_hook=unique_fields)
        except (ValueError, UnicodeDecodeError):
            raise CheckpointError("checkpoint JSON is invalid") from None
        keys = {"schema", "bindingSha256", "modelBudget", "records", "lifecycle", "inFlight", "complete", "checkpointSha256"}
        if (not isinstance(state, dict) or set(state) != keys
                or state["schema"] != "projectbuddy/assisted-review-checkpoint@1" or state["complete"] is not False
                or type(state["inFlight"]) is not bool or not isinstance(state["records"], list)
                or len(state["records"]) > 3 or not isinstance(state["modelBudget"], dict)):
            raise CheckpointError("checkpoint schema/state is invalid; partial data is not a final receipt")
        digest = state.pop("checkpointSha256")
        if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or hashlib.sha256(canonical(state)).hexdigest() != digest or state["bindingSha256"] != self.binding):
            raise CheckpointError("checkpoint integrity or approved input identity changed")
        state["checkpointSha256"] = digest
        self.digest = digest
        return state

    def save(self, records: list, lifecycle: dict, model_budget: dict, in_flight: bool = False) -> None:
        if self.lock is None:
            raise CheckpointError("checkpoint write requires the exclusive run lock")
        if self.path.exists() or self.path.is_symlink():
            prior = self._read()
            if self.state is None or prior["checkpointSha256"] != self.state["checkpointSha256"]:
                raise CheckpointError("checkpoint changed concurrently")
        state = {"schema": "projectbuddy/assisted-review-checkpoint@1", "bindingSha256": self.binding,
                 "modelBudget": model_budget, "records": records, "lifecycle": lifecycle,
                 "inFlight": in_flight, "complete": False}
        state["checkpointSha256"] = hashlib.sha256(canonical(state)).hexdigest()
        payload = canonical(state) + b"\n"
        if len(payload) > 1024 * 1024:
            raise CheckpointError("checkpoint exceeds the private record bound")
        descriptor, name = tempfile.mkstemp(prefix=".review-checkpoint-", dir=self.path.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self.state is None:
                os.link(temporary, self.path, follow_symlinks=False)
            else:
                os.replace(temporary, self.path)
            self.state = state
            self.digest = state["checkpointSha256"]
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def __exit__(self, exception_type, *_error):
        if self.lock is not None:
            descriptor = self.lock
            self.lock = None
            try:
                os.close(descriptor)
            except OSError:
                if exception_type is None:
                    raise
