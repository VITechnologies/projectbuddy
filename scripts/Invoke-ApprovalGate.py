#!/usr/bin/env python3
"""Invoke-ApprovalGate.py: submit and follow human-approved automation requests.

Implements ADR-0007 (`docs/architecture/adr/ADR-0007-human-approved-automation-requests.md`):
an agent, bot, or scheduled workflow submits one immutable request per
workflow run, a human required reviewer of the action class's protected
GitHub environment approves or rejects it, and the gated job verifies the
approval before any side effect. Setup and usage:
`docs/approval-gate/procedure.md`.

Usage:
    Invoke-ApprovalGate.py readiness --repo OWNER/NAME
    Invoke-ApprovalGate.py submit --repo OWNER/NAME --id ID --class noop --digest SHA256 \\
        --rationale @why.md --expected-effect @effect.md --rollback @rollback.md --tracking-issue N [--wait]
    Invoke-ApprovalGate.py status ID --repo OWNER/NAME [--wait]
    Invoke-ApprovalGate.py queue --repo OWNER/NAME [--repo ...]

Standard library only (Python 3.11+); a GitHub App token is signed with
the `openssl` command line.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from approval_gate.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
