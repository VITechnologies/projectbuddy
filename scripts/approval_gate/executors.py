"""Built-in executors. An action class names one; adopters cannot add code here.

An executor runs only after `verify.verify` passed, in the same gated job,
and it is the only code that may read the approval environment's secrets
(REQ-098).
"""
from __future__ import annotations

import os
from typing import Callable, Mapping, Optional

from .model import Request
from .registry import ActionClass

PROBE_SECRET = "APPROVAL_GATE_PROBE"

Executor = Callable[[Request, ActionClass, Mapping[str, str]], dict]


def noop(request: Request, action_class: ActionClass, environ: Mapping[str, str]) -> dict:
    """Known-answer executor: proves the gate end to end and writes nothing.

    It reports whether the approval environment's probe secret is visible, so
    the live proof can show the secret appears only after approval (TEST-100).
    The value itself is never read into the report.
    """
    return {
        "executor": "noop",
        "request_id": request.id,
        "request_digest": request.digest(),
        "environment": action_class.environment,
        "writes": 0,
        "probe_secret_present": bool(environ.get(PROBE_SECRET)),
    }


EXECUTORS: "dict[str, Executor]" = {"noop": noop}


def run(request: Request, action_class: ActionClass, environ: Optional[Mapping[str, str]] = None,
        executors: Optional["dict[str, Executor]"] = None) -> dict:
    table = executors if executors is not None else EXECUTORS
    executor = table.get(action_class.executor)
    if executor is None:
        raise KeyError(f"no executor named {action_class.executor!r}")
    return executor(request, action_class, os.environ if environ is None else environ)
