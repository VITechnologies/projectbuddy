"""Synthetic release-conveyor transition harness (REQ-085/REQ-086 prototype).

Evaluates records only. It never controls a VM, Docker, or GitHub. The
launcher, not this process, binds the running image to HARNESS_DIGEST.
Exit codes: 0 PASS, 1 FAIL, 2 UNKNOWN, 3 refused (unsafe environment or
invocation).
"""

import hashlib
import json
import os
import re
import sys

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
EXIT = {PASS: 0, FAIL: 1, UNKNOWN: 2}
REFUSED = 3

ORDER = ["D0", "V1", "D1", "V2", "D2"]
STATIONS = {"D0", "D1", "D2"}
STAGE_LANES = {
    "V1": {"ubuntu-linux", "ubuntu-windows-public"},
    "V2": {"windows-linux"},
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

STATION_FIELDS = {"schema", "conveyorId", "candidate", "station", "harnessDigest",
                  "predecessorDigest", "verdict", "reasons"}
STAGE_FIELDS = {"schema", "conveyorId", "candidate", "stage", "predecessorDigest", "lanes"}
LANE_FIELDS = {"lane", "verdict", "cleanup", "evidenceDigest"}
LUNIT_FIELDS = {"schema", "conveyorId", "candidate", "linux", "windows"}
ALLOWED_ENV = {"PATH", "HOSTNAME", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "HOME"}
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")
HYPERVISOR_DEVICES = ("/dev/vboxdrv", "/dev/vboxdrvu", "/dev/vboxnetctl", "/dev/kvm")


class Verdict(Exception):
    def __init__(self, verdict, reason):
        super().__init__(reason)
        self.verdict = verdict
        self.reason = reason


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(obj):
    return "sha256:" + hashlib.sha256(canonical(obj)).hexdigest()


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise Verdict(FAIL, "duplicate-field")
    return dict(pairs)


def load(directory, name):
    path = os.path.join(directory, name + ".json")
    if not os.path.isfile(path):
        raise Verdict(UNKNOWN, "missing-" + name)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_no_duplicates)
    except Verdict:
        raise
    except (ValueError, UnicodeDecodeError):
        raise Verdict(FAIL, "malformed-" + name)


def _exact_fields(record, fields, name):
    if not isinstance(record, dict) or set(record) != fields:
        raise Verdict(FAIL, "fields-" + name)


def _bind(record, ctx, name):
    if record["conveyorId"] != ctx["conveyorId"]:
        raise Verdict(FAIL, "conveyor-mismatch-" + name)
    if record["candidate"] != ctx["candidate"]:
        raise Verdict(FAIL, "candidate-mismatch-" + name)


def check_station_record(record, ctx, station, predecessor):
    _exact_fields(record, STATION_FIELDS, station)
    if record["schema"] != "conveyor-station/v1" or record["station"] != station:
        raise Verdict(FAIL, "identity-" + station)
    _bind(record, ctx, station)
    if record["verdict"] != PASS:
        raise Verdict(FAIL if record["verdict"] == FAIL else UNKNOWN, "halted-at-" + station)
    if record["harnessDigest"] != ctx["harnessDigest"]:
        raise Verdict(FAIL, "harness-mismatch-" + station)
    expected = None if predecessor is None else digest(predecessor)
    if record["predecessorDigest"] != expected:
        raise Verdict(FAIL, "chain-break-" + station)


def check_stage_record(record, ctx, stage, predecessor):
    _exact_fields(record, STAGE_FIELDS, stage)
    if record["schema"] != "conveyor-stage/v1" or record["stage"] != stage:
        raise Verdict(FAIL, "identity-" + stage)
    _bind(record, ctx, stage)
    if record["predecessorDigest"] != digest(predecessor):
        raise Verdict(FAIL, "chain-break-" + stage)
    lanes = record["lanes"]
    if not isinstance(lanes, list):
        raise Verdict(FAIL, "fields-" + stage)
    names = []
    for lane in lanes:
        _exact_fields(lane, LANE_FIELDS, stage + "-lane")
        names.append(lane["lane"])
    if len(names) != len(set(names)):
        raise Verdict(FAIL, "duplicate-lane-" + stage)
    if set(names) - STAGE_LANES[stage]:
        raise Verdict(FAIL, "foreign-lane-" + stage)
    if set(names) != STAGE_LANES[stage]:
        raise Verdict(UNKNOWN, "missing-lane-" + stage)
    for lane in sorted(lanes, key=lambda item: item["lane"]):
        if lane["verdict"] == FAIL:
            raise Verdict(FAIL, "lane-failed-" + lane["lane"])
        if lane["verdict"] != PASS:
            raise Verdict(UNKNOWN, "lane-unknown-" + lane["lane"])
        if not isinstance(lane["evidenceDigest"], str) or not DIGEST_RE.match(lane["evidenceDigest"]):
            raise Verdict(UNKNOWN, "evidence-" + lane["lane"])
        if lane["cleanup"] != "complete":
            raise Verdict(UNKNOWN, "cleanup-pending-" + lane["lane"])


def check_lunit(record, ctx):
    _exact_fields(record, LUNIT_FIELDS, "lunit")
    if record["schema"] != "conveyor-lunit/v1":
        raise Verdict(FAIL, "identity-lunit")
    _bind(record, ctx, "lunit")
    for side in ("linux", "windows"):
        if record[side] == FAIL:
            raise Verdict(FAIL, "lunit-failed-" + side)
        if record[side] != PASS:
            raise Verdict(UNKNOWN, "lunit-unknown-" + side)


def evaluate(station, ctx, inputs, progress):
    """Re-verify the chain before `station`; progress["last"] is the last verified record."""
    upto = ORDER.index(station)
    for name in ORDER[upto:]:
        if os.path.exists(os.path.join(inputs, name + ".json")):
            raise Verdict(FAIL, "out-of-order-" + name)
    check_lunit(load(inputs, "lunit"), ctx)
    previous = None
    for name in ORDER[:upto]:
        record = load(inputs, name)
        if name in STATIONS:
            check_station_record(record, ctx, name, previous)
        else:
            check_stage_record(record, ctx, name, previous)
        previous = record
        progress["last"] = record
    return previous


def emit(station, ctx, predecessor, verdict, reason):
    return {
        "schema": "conveyor-station/v1",
        "conveyorId": ctx["conveyorId"],
        "candidate": ctx["candidate"],
        "station": station,
        "harnessDigest": ctx["harnessDigest"],
        "predecessorDigest": None if predecessor is None else digest(predecessor),
        "verdict": verdict,
        "reasons": [reason] if reason else [],
    }


def _read_only_tree(path):
    if not os.path.isdir(path) or not os.statvfs(path).f_flag & os.ST_RDONLY:
        return False
    for entry in os.scandir(path):
        if os.access(entry.path, os.W_OK) or not os.statvfs(entry.path).f_flag & os.ST_RDONLY:
            return False
    return True


def environment_problems(inputs, outputs, environ=None, net_dir="/sys/class/net",
                         sockets=DOCKER_SOCKETS, devices=HYPERVISOR_DEVICES,
                         read_only=_read_only_tree):
    """Defense in depth only; the launcher must enforce and attest the sandbox."""
    environ = os.environ if environ is None else environ
    problems = []
    if set(environ) - ALLOWED_ENV:
        problems.append("unexpected-env")
    if any(os.path.exists(path) for path in sockets):
        problems.append("docker-socket")
    if any(os.path.exists(path) for path in devices):
        problems.append("hypervisor-device")
    if not os.path.isdir(net_dir) or set(os.listdir(net_dir)) - {"lo"}:
        problems.append("network-unproven")
    if not read_only(inputs):
        problems.append("input-not-read-only")
    if not os.path.isdir(outputs) or not os.access(outputs, os.W_OK):
        problems.append("output-unwritable")
    return problems


def run(station, ctx, inputs, outputs):
    progress = {"last": None}
    try:
        predecessor = evaluate(station, ctx, inputs, progress)
        verdict, reason = PASS, ""
    except Verdict as outcome:
        predecessor, verdict, reason = progress["last"], outcome.verdict, outcome.reason
    record = emit(station, ctx, predecessor, verdict, reason)
    with open(os.path.join(outputs, station + ".json"), "x", encoding="utf-8") as handle:
        handle.write(canonical(record).decode() + "\n")
    return record


def main(argv, environ=None, net_dir="/sys/class/net", sockets=DOCKER_SOCKETS,
         devices=HYPERVISOR_DEVICES, read_only=_read_only_tree):
    if len(argv) != 6:
        print("usage: harness.py STATION CONVEYOR_ID CANDIDATE HARNESS_DIGEST INPUT_DIR OUTPUT_DIR",
              file=sys.stderr)
        return REFUSED
    station, conveyor_id, candidate, harness_digest, inputs, outputs = argv
    if (station not in STATIONS or not ID_RE.match(conveyor_id) or not SHA_RE.match(candidate)
            or not DIGEST_RE.match(harness_digest)):
        print("refused: invalid-invocation", file=sys.stderr)
        return REFUSED
    problems = environment_problems(inputs, outputs, environ, net_dir, sockets, devices, read_only)
    if problems:
        print("refused: " + ",".join(problems), file=sys.stderr)
        return REFUSED
    if os.path.exists(os.path.join(outputs, station + ".json")):
        print("refused: output-exists", file=sys.stderr)
        return REFUSED
    ctx = {"conveyorId": conveyor_id, "candidate": candidate, "harnessDigest": harness_digest}
    record = run(station, ctx, inputs, outputs)
    reason = record["reasons"][0] if record["reasons"] else ""
    print(station + " " + record["verdict"] + ((" " + reason) if reason else ""))
    return EXIT[record["verdict"]]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
