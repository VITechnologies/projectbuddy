"""Command line for the lane executor: teardown by UUID and the teardown receipt verifier."""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys

from lane_box.common import LaneBoxError

from .teardown import ReceiptNotWritten, Request, VirtualBox, plan, teardown, verify_receipt

LOCK_DIRECTORY = "projectbuddy-lane-locks"
REMOVAL_OUTCOMES = ("removed", "absent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Invoke-Lane.py",
        description="Ubuntu lane executor: teardown and receipts (docs/vm-provisioning/ubuntu-lane.md).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    down = commands.add_parser("teardown", help="host: tear down one lane instance by UUID (default: print the plan)")
    down.add_argument("--uuid", required=True, help="the instance's VM UUID; a VM name is refused")
    down.add_argument("--run-dir", type=pathlib.Path, required=True)
    down.add_argument("--box", type=pathlib.Path, required=True)
    down.add_argument("--box-sha256", required=True)
    down.add_argument("--receipt-out", type=pathlib.Path, required=True)
    down.add_argument("--lock-dir", type=pathlib.Path, help=f"default: $XDG_RUNTIME_DIR/{LOCK_DIRECTORY}")
    down.add_argument("--vboxmanage")
    down.add_argument("--run", action="store_true", help="tear down for real (default: print the plan)")

    verify = commands.add_parser("verify-teardown", help="host: check a lane teardown receipt")
    verify.add_argument("receipt", type=pathlib.Path)
    verify.add_argument("--uuid")
    verify.add_argument("--box-sha256")
    return parser


def _lock_dir(value: pathlib.Path | None) -> pathlib.Path:
    if value is not None:
        return value
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not os.path.isabs(runtime):
        raise LaneBoxError("XDG_RUNTIME_DIR is not an absolute path; pass --lock-dir")
    path = pathlib.Path(runtime) / LOCK_DIRECTORY
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _teardown(arguments: argparse.Namespace) -> int:
    vboxmanage = shutil.which(arguments.vboxmanage or "VBoxManage")
    if not vboxmanage:
        print("Lane teardown: FAIL: VBoxManage is missing", file=sys.stderr)
        return 127
    request = Request(
        uuid=arguments.uuid, run_dir=arguments.run_dir, box=arguments.box, box_sha256=arguments.box_sha256,
        lock_dir=_lock_dir(arguments.lock_dir), receipt_out=arguments.receipt_out,
    )
    vbox = VirtualBox(vboxmanage)
    if not arguments.run:
        print("\n".join(plan(request, vbox)))
        return 0
    receipt, changed = teardown(request, vbox)
    for failure in receipt["failures"]:
        print(f"Lane teardown: FAIL: {failure}", file=sys.stderr)
    print(f"Lane teardown: {receipt['outcome'].upper()}: {receipt['instance']['uuid']}: {arguments.receipt_out}")
    if receipt["outcome"] in REMOVAL_OUTCOMES:
        return 0
    return 1 if changed else 2


def _verify(arguments: argparse.Namespace) -> int:
    receipt = verify_receipt(arguments.receipt, arguments.uuid, arguments.box_sha256)
    summary = f"{receipt['outcome']}: {receipt['instance']['uuid']}"
    if receipt["outcome"] in REMOVAL_OUTCOMES:
        print(f"Lane teardown receipt: PASS: {summary}")
        return 0
    print(f"Lane teardown receipt: FAIL: the receipt is consistent but records {summary}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    label = {"teardown": "Lane teardown", "verify-teardown": "Lane teardown receipt"}[arguments.command]
    try:
        if arguments.command == "teardown":
            return _teardown(arguments)
        return _verify(arguments)
    except ReceiptNotWritten as error:
        print(f"{label}: FAIL: {error}", file=sys.stderr)
        return 1 if error.changed else 2
    except LaneBoxError as error:
        print(f"{label}: FAIL: {error}", file=sys.stderr)
        return 2
