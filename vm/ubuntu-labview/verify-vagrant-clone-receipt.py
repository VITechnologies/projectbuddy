#!/usr/bin/env python3
"""Verify a LabVIEW appliance clone receipt with the neutral verifier and ``clone-profile.json``.

``projectbuddy/ubuntu-vagrant-clone@2`` receipts name their profile's SHA-256 and verify against
``--profile``. ``@1`` receipts name none, so they verify only against the frozen
``clone-profile.v1.json``, pinned below, which states the ``@1`` contract; ``--profile`` does not
apply to them.
"""
import argparse
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parents[1] / "scripts"))

from lane_box.common import LaneBoxError  # noqa: E402
from vagrant_clone.profile import load_profile  # noqa: E402
from vagrant_clone.receipt import verify_receipt  # noqa: E402

# The frozen profile that reproduces the checks behind @1 receipts, and its SHA-256.
V1_PROFILE = SCRIPT_DIR / "clone-profile.v1.json"
V1_PROFILE_SHA256 = "b761482d0b1be63a8c7b2c0da1e0e3c0b5aa5ef24d890e6ef36e410200f30ab8"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=pathlib.Path)
    parser.add_argument("--box-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--profile", type=pathlib.Path, default=SCRIPT_DIR / "clone-profile.json")
    arguments = parser.parse_args()
    try:
        receipt = verify_receipt(
            arguments.receipt, profile=load_profile(arguments.profile), box_receipt=arguments.box_receipt,
            evidence_dir=arguments.evidence_dir, name=arguments.vm_name, uuid=arguments.vm_uuid,
            v1_profile=load_profile(V1_PROFILE), v1_profile_sha256=V1_PROFILE_SHA256,
        )
    except LaneBoxError as error:
        print(f"Vagrant clone receipt: FAIL: {error}", file=sys.stderr)
        return 2
    print(f"Vagrant clone receipt: PASS: {arguments.vm_name} ({receipt['clone']['vm']['uuid']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
