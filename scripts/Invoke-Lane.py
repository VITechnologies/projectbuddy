#!/usr/bin/env python3
"""Invoke-Lane.py: the Ubuntu lane executor's teardown and receipt verifier.

Implements part of VITechnologies/projectbuddy#185 (REQ-102). Procedure:
`docs/vm-provisioning/ubuntu-lane.md`.

Usage:
    Invoke-Lane.py teardown --uuid UUID --run-dir DIR --box BOX --box-sha256 SHA256 \\
        --receipt-out RECEIPT [--lock-dir DIR] [--run]                 (host)
    Invoke-Lane.py verify-teardown RECEIPT [--uuid UUID] [--box-sha256 SHA256]   (host)

Standard library only (Python 3.11+, POSIX hosts).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lane.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
