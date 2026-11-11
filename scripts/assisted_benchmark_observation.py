"""Minimized, explicitly non-success model-review observations."""
from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path

from assisted_benchmark_contract import validate_review_lifecycle

SCHEMA = "projectbuddy/assisted-review-refusal@1"
PHASES = {"input-validation", "orchestration", "mcp-context", "capture-validation", "review-preflight",
          "checkpoint-validation", "identity-preflight", "model-metadata", "inference", "identity-postflight", "finalization"}
KINDS = {"deadline", "cancelled", "transport", "validation", "input", "internal"}


class ObservationError(ValueError):
    pass


def failure_kind(error: BaseException) -> str:
    if isinstance(error, KeyboardInterrupt) or type(error).__name__ == "ReviewCancelledError":
        return "cancelled"
    if isinstance(error, TimeoutError) or type(error).__name__ == "ReviewDeadlineError":
        return "deadline"
    if type(error).__name__ == "ReviewTransportError":
        return "transport"
    if isinstance(error, OSError):
        return "input"
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "validation"
    return "internal"


class RefusalObserver:
    def __init__(self):
        self.phase = "input-validation"
        self.lifecycle = None
        self.limits = None

    def configure_limits(self, policy) -> None:
        self.limits = validate_review_lifecycle(policy) if policy is not None else None

    def begin_review(self) -> None:
        self.lifecycle = None
        self.limits = None
        self.phase = "review-preflight"

    def mark(self, phase: str) -> None:
        if phase not in PHASES:
            raise ObservationError("unsupported observation phase")
        self.phase = phase
        if self.lifecycle is not None:
            self.lifecycle.phase = phase

    def bind(self, lifecycle) -> None:
        self.lifecycle = lifecycle
        self.phase = lifecycle.phase

    def snapshot(self, error: BaseException, exit_code: int) -> dict:
        kind = failure_kind(error)
        lifecycle = self.lifecycle
        progress = lifecycle.progress() if lifecycle is not None else {
            "requestCount": 0, "retryCount": 0, "reservedUnknownRequestCount": 0, "consumedSeconds": 0.0}
        elapsed = progress["consumedSeconds"]
        record = {
            "schema": SCHEMA, "complete": False, "outcome": "cancelled" if kind == "cancelled" else "refused",
            "exitCode": exit_code, "failureKind": kind, "phase": lifecycle.phase if lifecycle is not None else self.phase,
            "limits": dict(lifecycle.policy) if lifecycle is not None else self.limits,
            "requestCount": progress["requestCount"], "retryCount": progress["retryCount"],
            "reservedUnknownRequestCount": progress["reservedUnknownRequestCount"],
            "consumedSeconds": elapsed if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) else None,
            "requestMayHaveBeenInFlight": lifecycle.request_may_have_been_in_flight if lifecycle is not None else False,
            "lastRequestSha256": lifecycle.last_request_sha256 if lifecycle is not None else None,
            "serverCompletion": "unknown" if progress["requestCount"] else "not-started",
        }
        validate_observation(record)
        return record


def validate_observation(record: object) -> None:
    keys = {"schema", "complete", "outcome", "exitCode", "failureKind", "phase", "limits", "requestCount", "retryCount",
            "reservedUnknownRequestCount", "consumedSeconds", "requestMayHaveBeenInFlight", "lastRequestSha256", "serverCompletion"}
    if (not isinstance(record, dict) or set(record) != keys or record["schema"] != SCHEMA or record["complete"] is not False
            or record["outcome"] not in {"refused", "cancelled"} or record["failureKind"] not in KINDS
            or record["phase"] not in PHASES or type(record["exitCode"]) is not int or record["exitCode"] not in {1, 2, 130}
            or type(record["requestMayHaveBeenInFlight"]) is not bool
            or record["serverCompletion"] not in {"unknown", "not-started"}):
        raise ObservationError("invalid non-success observation")
    for key in ("requestCount", "retryCount", "reservedUnknownRequestCount"):
        if type(record[key]) is not int or not 0 <= record[key] <= 256:
            raise ObservationError("invalid observation counter")
    elapsed = record["consumedSeconds"]
    if elapsed is not None and (type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0):
        raise ObservationError("invalid observation elapsed time")
    digest = record["lastRequestSha256"]
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        raise ObservationError("invalid observation request hash")
    if record["limits"] is not None:
        validate_review_lifecycle(record["limits"])


def write_private_observation(path: Path, record: dict) -> None:
    validate_observation(record)
    if os.name != "posix":
        raise ObservationError("private observation custody is unsupported on this platform")
    output = Path(path)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ObservationError("observation requires a new absolute non-symlink file")
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = parent.stat()
    if parent.resolve(strict=True) != parent or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
        raise ObservationError("observation directory requires private current-user custody")
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(payload) > 65536:
        raise ObservationError("observation exceeds the bounded record size")
    descriptor, name = tempfile.mkstemp(prefix=".review-refusal-", dir=parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output, follow_symlinks=False)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
