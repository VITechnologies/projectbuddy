"""Human-approved automation requests through protected GitHub environments.

Implements ADR-0007 (`docs/architecture/adr/ADR-0007-human-approved-automation-requests.md`)
and the wiki's SYS-REQ-028 and REQ-094 to REQ-100. A requester (an agent,
bot, or scheduled workflow) submits one immutable request per workflow run;
a human required reviewer of the action class's environment approves or
rejects it; the gated job verifies the approval before any side effect; and
every decision leaves a durable record on the request's tracking issue.

The entry point is `scripts/Invoke-ApprovalGate.py`. Usage and setup are in
`docs/approval-gate/procedure.md`.
"""
from __future__ import annotations

TOOL_VERSION = "0.1.0"

REQUEST_SCHEMA = "approval-gate/request@1"
REGISTRY_SCHEMA = "approval-gate/classes@1"
RESULT_SCHEMA = "approval-gate/result@1"
RECORD_SCHEMA = "approval-gate/record@1"
READINESS_SCHEMA = "approval-gate/readiness@1"

RUN_TITLE_PREFIX = "Approval request "
STAGING_BRANCH_PREFIX = "requests/"
DEFAULT_WORKFLOW = "approval-request.yml"
DEFAULT_REGISTRY_PATH = ".github/approval-gate/classes.json"
