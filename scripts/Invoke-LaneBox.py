#!/usr/bin/env python3
"""Invoke-LaneBox.py: build and verify the LabVIEW-free Ubuntu lane box.

Implements VITechnologies/projectbuddy#183 (REQ-101, REQ-103). Procedure:
`docs/vm-provisioning/ubuntu-lane-box.md`.

Usage:
    Invoke-LaneBox.py validate-profile PROFILE
    Invoke-LaneBox.py install-toolchain --profile PROFILE          (guest, as root)
    Invoke-LaneBox.py readiness --profile PROFILE                  (guest, as root)
    Invoke-LaneBox.py package --profile PROFILE --vm-name NAME --vm-uuid UUID \\
        --snapshot-name NAME --snapshot-uuid UUID --evidence-dir DIR --output BOX \\
        --receipt-out RECEIPT --vagrant-home DIR --temp-dir DIR [--run]   (host)
    Invoke-LaneBox.py verify-receipt RECEIPT --box BOX --profile PROFILE \\
        --base-receipt FILE --readiness-receipt FILE                  (host)

Standard library only (Python 3.11+).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lane_box.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
