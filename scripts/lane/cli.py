"""Command line for the lane executor: the dry-run plan, teardown by UUID, and the teardown receipt verifier."""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys

from lane_box.common import LaneBoxError

from . import plan as lane_plan
from .teardown import ReceiptNotWritten, Request, VirtualBox, plan, teardown, verify_receipt

LOCK_DIRECTORY = "projectbuddy-lane-locks"
REMOVAL_OUTCOMES = ("removed", "absent")
ENGINE = pathlib.Path(__file__).resolve().parents[2]
EMBEDDED_VAGRANTFILE = ENGINE / "vm" / "ubuntu-lane" / "Vagrantfile.box"
LABELS = {"plan": "Lane plan", "teardown": "Lane teardown", "verify-teardown": "Lane teardown receipt"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Invoke-Lane.py",
        description="Ubuntu lane executor: plan, teardown, and receipts (docs/vm-provisioning/ubuntu-lane.md).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    dry = commands.add_parser("plan", help="host: check every input and write a dry-run lane plan; changes nothing")
    dry.add_argument("--declaration", type=pathlib.Path, required=True, help="the workload declaration JSON")
    dry.add_argument("--candidate-repo", type=pathlib.Path, required=True,
                     help="the host's clone that holds the candidate commit")
    dry.add_argument("--box", type=pathlib.Path, required=True)
    dry.add_argument("--box-receipt", type=pathlib.Path, required=True)
    dry.add_argument("--profile", type=pathlib.Path, required=True)
    dry.add_argument("--base-receipt", type=pathlib.Path, required=True)
    dry.add_argument("--readiness-receipt", type=pathlib.Path, required=True)
    dry.add_argument("--embedded-vagrantfile", type=pathlib.Path, default=EMBEDDED_VAGRANTFILE)
    dry.add_argument("--work-dir", type=pathlib.Path, required=True, help="holds each instance's run directory")
    dry.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    dry.add_argument("--records-dir", type=pathlib.Path, required=True,
                     help="holds the teardown receipts of earlier lane instances")
    dry.add_argument("--plan-out", type=pathlib.Path, required=True)
    dry.add_argument("--ssh-port", type=int, help="host port for the guest's SSH forward (default: a free port)")
    dry.add_argument("--vboxmanage")
    dry.add_argument("--vagrant")
    dry.add_argument("--git")

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


def _plan(arguments: argparse.Namespace) -> int:
    tools = {name: shutil.which(getattr(arguments, name) or default)
             for name, default in (("vboxmanage", "VBoxManage"), ("vagrant", "vagrant"), ("git", "git"))}
    missing = [name for name, path in tools.items() if not path]
    if missing:
        print(f"Lane plan: FAIL: missing {', '.join(missing)}", file=sys.stderr)
        return 127
    request = lane_plan.Request(
        declaration=arguments.declaration, candidate_repo=arguments.candidate_repo, box=arguments.box,
        box_receipt=arguments.box_receipt, profile=arguments.profile, base_receipt=arguments.base_receipt,
        readiness_receipt=arguments.readiness_receipt, embedded_vagrantfile=arguments.embedded_vagrantfile,
        work_dir=arguments.work_dir, evidence_dir=arguments.evidence_dir, records_dir=arguments.records_dir,
        plan_out=arguments.plan_out, engine=ENGINE, ssh_port=arguments.ssh_port,
    )
    lane_plan.check_request(request)
    value = lane_plan.make_plan(request, lane_plan.Host(**tools), os.environ)
    lane_plan.write_plan(request, value)
    for refusal in value["refusals"]:
        print(f"Lane plan: REFUSED: {refusal['rule']}: {refusal['detail']}", file=sys.stderr)
    print(f"Lane plan: {value['outcome'].upper()}: {value['instance']['name']}: {arguments.plan_out}")
    return 0 if value["outcome"] == "ready" else 2


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
    label = LABELS[arguments.command]
    try:
        if arguments.command == "plan":
            return _plan(arguments)
        if arguments.command == "teardown":
            return _teardown(arguments)
        return _verify(arguments)
    except ReceiptNotWritten as error:
        print(f"{label}: FAIL: {error}", file=sys.stderr)
        return 1 if error.changed else 2
    except LaneBoxError as error:
        print(f"{label}: FAIL: {error}", file=sys.stderr)
        return 2
