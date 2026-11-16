#!/usr/bin/env python3
import argparse
import os
import pathlib
import re
import subprocess
import sys
import uuid


VALID_PHASES = {
    "validate",
    "inspect-public",
    "console-fingerprint",
    "repair-ssh-console",
    "restart-public",
    "repair-public",
    "resume-public",
    "provision-public",
    "complete-activation",
    "register-runner",
    "package-build",
}
WINDOWS_ONLY_PHASES = {
    "inspect-public",
    "console-fingerprint",
    "repair-ssh-console",
    "restart-public",
    "repair-public",
    "resume-public",
}
LINUX_ONLY_PHASES = {"complete-activation", "register-runner", "package-build"}


def validate_dispatch() -> None:
    runner_os = os.environ["RUNNER_OS"].lower()
    requested_os = os.environ["INPUT_RUNNER_OS"].lower()
    if runner_os != requested_os:
        raise SystemExit(f"Runner OS {runner_os!r} does not match requested OS {requested_os!r}")
    phase = os.environ["INPUT_PHASE"]
    if phase not in VALID_PHASES:
        raise SystemExit(f"Unsupported provisioning phase {phase!r}")
    if phase in WINDOWS_ONLY_PHASES and requested_os != "windows":
        raise SystemExit(f"Phase {phase!r} is only supported on Windows runners")
    if phase in LINUX_ONLY_PHASES and requested_os != "linux":
        raise SystemExit(f"Phase {phase!r} is only supported on Linux runners")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", os.environ["INPUT_VM_NAME"]):
        raise SystemExit("vm_name contains unsupported characters")
    if phase in {
        "inspect-public", "resume-public", "console-fingerprint", "repair-ssh-console", "restart-public",
        "repair-public", "complete-activation", "register-runner", "package-build",
    }:
        try:
            uuid.UUID(os.environ["INPUT_VM_UUID"])
        except ValueError:
            raise SystemExit("vm_uuid must be a UUID for public inspection") from None
    if phase == "package-build":
        try:
            uuid.UUID(os.environ["INPUT_SNAPSHOT_UUID"])
        except ValueError:
            raise SystemExit("snapshot_uuid must be a UUID for package-build") from None
    if phase in {"repair-ssh-console", "restart-public", "repair-public"} and not re.fullmatch(
        r"SHA256:[A-Za-z0-9+/]{20,}={0,2}", os.environ["INPUT_SSH_HOST_FINGERPRINT"]
    ):
        raise SystemExit("ssh_host_fingerprint must be an OpenSSH SHA-256 fingerprint for repair")
    storage_root = os.environ["INPUT_STORAGE_ROOT"]
    path_type = pathlib.PureWindowsPath if requested_os == "windows" else pathlib.PurePosixPath
    if not storage_root.strip() or not path_type(storage_root).is_absolute():
        raise SystemExit("storage_root must be an absolute path for the selected runner OS")
    if phase == "provision-public":
        iso_path = os.environ["INPUT_ISO_PATH"]
        if not iso_path.strip() or not path_type(iso_path).is_absolute():
            raise SystemExit("iso_path must be an absolute path for public provisioning")
    try:
        ssh_port = int(os.environ["INPUT_SSH_HOST_PORT"])
    except ValueError:
        raise SystemExit("ssh_host_port must be an integer") from None
    if not 1024 <= ssh_port <= 65535:
        raise SystemExit("ssh_host_port must be from 1024 through 65535")


def validate_public_plan() -> None:
    vm_name = os.environ["INPUT_VM_NAME"]
    storage_root = pathlib.Path(os.environ["INPUT_STORAGE_ROOT"])
    runner_temp = pathlib.Path(os.environ["RUNNER_TEMP"])
    subprocess.run(
        [
            sys.executable,
            # This engine's own self-dispatch runs against its own checkout
            # root (vm/ubuntu-labview/ lives at the top level here). A
            # consumer vendoring this script alongside its own copy of
            # provision-ubuntu-vm.yml (same pattern as add-project.yml --
            # see README.md's "Consumption model" section) adjusts this path
            # to its own pinned engine checkout location (e.g.
            # ".projectbuddy/vm/...").
            "vm/ubuntu-labview/complete-public-provisioning.py",
            "--vm-name", vm_name,
            "--vm-uuid", "00000000-0000-4000-8000-000000000001",
            "--guest-password-file", str(runner_temp / "guest-password"),
            "--evidence-dir", str(storage_root / "evidence" / vm_name),
            "--known-hosts", str(runner_temp / f"{vm_name}-known-hosts"),
            "--ssh-port", os.environ["INPUT_SSH_HOST_PORT"],
            "--dry-run",
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("dispatch", "public-plan"))
    arguments = parser.parse_args()
    if arguments.mode == "dispatch":
        validate_dispatch()
    else:
        validate_public_plan()


if __name__ == "__main__":
    main()