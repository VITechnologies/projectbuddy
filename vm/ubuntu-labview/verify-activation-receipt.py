#!/usr/bin/env python3
import argparse
import json
import pathlib
import re
import sys
import uuid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=pathlib.Path)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--boot-id", required=True)
    parser.add_argument("--snapshot-uuid", required=True)
    arguments = parser.parse_args()

    try:
        receipt = json.loads(arguments.receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"activation receipt: FAIL: {error}", file=sys.stderr)
        return 2

    errors = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(receipt.get("schema") == "projectbuddy/ubuntu-activation@1", "unexpected schema")
    require(receipt.get("outcome") == "PASS", "outcome is not PASS")
    require(receipt.get("failures") == [], "receipt contains failures")
    evidence_hashes = receipt.get("evidenceSha256", {})
    for name in ("knownAnswerCapture", "runtimeState", "vipmAbout", "vipmLabview", "vipmInstalled"):
        require(bool(re.fullmatch(r"[a-f0-9]{64}", str(evidence_hashes.get(name, "")))), f"{name} SHA-256 is invalid")

    vm = receipt.get("vm", {})
    require(vm.get("name") == arguments.vm_name, "receipt does not match the expected VM name")
    require(vm.get("uuid") == arguments.vm_uuid, "receipt does not match the expected VM UUID")
    require(vm.get("bootId") == arguments.boot_id, "receipt does not match the expected boot ID")
    for field in ("uuid", "bootId"):
        try:
            uuid.UUID(str(vm.get(field)))
        except (ValueError, AttributeError):
            errors.append(f"VM {field} is invalid")
    snapshot = vm.get("snapshot", {})
    require(snapshot.get("name") == "labview2026-q3-pro-activated-ready", "unexpected snapshot name")
    require(snapshot.get("uuid") == arguments.snapshot_uuid, "receipt does not match the expected snapshot UUID")
    try:
        uuid.UUID(str(snapshot.get("uuid")))
    except (ValueError, AttributeError):
        errors.append("snapshot UUID is invalid")

    require(receipt.get("os") == {"distribution": "ubuntu", "version": "24.04", "codename": "noble"}, "unexpected guest OS")
    require(receipt.get("security", {}).get("automaticLoginEnabled") is False, "automatic login remains enabled")

    labview = receipt.get("labview", {})
    require(labview.get("edition") == "Professional", "LabVIEW edition is not Professional")
    require(labview.get("package") == "ni-labview-2026-pro", "unexpected LabVIEW package")
    require(labview.get("packageVersion") == "26.3.1.49165-0+f13", "unexpected LabVIEW package version")
    require(labview.get("appPath") == "/usr/local/natinst/LabVIEW-2026-64/labviewprofull", "unexpected LabVIEW executable")
    require(labview.get("viServerPort") == 3363, "unexpected VI Server port")
    probe = labview.get("knownAnswerProbe", {})
    require(probe.get("inputs") == [20, 22], "unexpected LabVIEW probe inputs")
    require(probe.get("expectedOutput") == 42, "unexpected LabVIEW expected output")
    require(probe.get("actualOutput") == 42, "LabVIEW known-answer output is incorrect")
    require(probe.get("exitCode") == 0, "LabVIEW known-answer probe failed")
    require(isinstance(probe.get("wallMs"), int) and probe["wallMs"] > 0, "LabVIEW probe duration is invalid")
    require(probe.get("displayMode") in {"active-graphical-seat", "xvfb"}, "LabVIEW probe had no graphical display")
    output = probe.get("output", "")
    require(bool(re.search(r"Operation output:\s*42\s+RunVI operation succeeded\.", output)), "LabVIEW success output is missing")

    vipm = receipt.get("vipm", {})
    require(vipm.get("version") == "26.3.2", "unexpected VIPM version")
    require(vipm.get("buildNumber") == "4056", "unexpected VIPM build number")
    require(vipm.get("edition") == "Professional", "VIPM edition is not Professional")
    require(vipm.get("validThrough") == "Perpetual", "VIPM activation is not perpetual")
    require(vipm.get("validActivationCode") is True, "VIPM activation is invalid")
    target = vipm.get("labviewTarget", {})
    require(target.get("version") == "2026", "VIPM did not select LabVIEW 2026")
    require(target.get("bitness") == 64, "VIPM did not select 64-bit LabVIEW")
    require(target.get("appPath") == labview.get("appPath"), "VIPM discovered a different LabVIEW executable")
    require(target.get("viServerPort") == 3363, "VIPM discovered an unexpected VI Server port")
    require(vipm.get("installedLookupAvailable") is True, "VIPM installed-package lookup is unavailable")

    if errors:
        for error in errors:
            print(f"activation receipt: FAIL: {error}", file=sys.stderr)
        return 2
    print(f"activation receipt: PASS: {vm['name']} ({vm['bootId']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())