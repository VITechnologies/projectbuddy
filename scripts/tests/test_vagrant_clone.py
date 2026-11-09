"""Vagrant clone check (VITechnologies/projectbuddy#185, REQ-103, TEST-105): profile, core, receipt, live flow."""
from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lane_box.common import LaneBoxError, sha256_file, verify_hash_file  # noqa: E402
from vagrant_clone import core, live  # noqa: E402
from vagrant_clone import profile as clone_profile  # noqa: E402
from vagrant_clone import receipt as clone_receipt  # noqa: E402
from vagrant_clone.core import CloneError  # noqa: E402
from vagrant_clone.profile import ProfileError  # noqa: E402
from vagrant_clone.receipt import ReceiptError  # noqa: E402

REPOSITORY = SCRIPTS.parent
APPLIANCE = REPOSITORY / "vm" / "ubuntu-labview"
PROFILE_PATH = APPLIANCE / "clone-profile.json"
CLONE_ADAPTER = APPLIANCE / "verify-vagrant-clone.py"
RECEIPT_ADAPTER = APPLIANCE / "verify-vagrant-clone-receipt.py"
UUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
SOURCE_UUID = "16fd2706-8baf-433b-82eb-8c7fada847da"
ACTIVATED_UUID = "2b1c3d4e-5f60-4718-9a2b-3c4d5e6f7081"
PACKAGE_UUID = "3c2d4e5f-6071-4829-8b3c-4d5e6f708192"
BOOT = "0f8fad5b-d9cb-469f-a165-70867728950e"
NAME = "clone-1"
PORT = 2222
FINGERPRINT = "SHA256:" + "A" * 43
BOX_SHA256 = "a" * 64
RUNNER = "actions.runner.org-repo.runner-1.service"
ENROLL = Path("/engine/enroll-ssh-host-key.py")
KNOWN_ANSWER, RUNTIME, ABOUT, TARGETS, INSTALLED = (
    "activation-known-answer-capture.json", "sandbox-runtime-state.json", "sandbox-vipm-about.json",
    "sandbox-vipm-labview.json", "sandbox-vipm-installed.json")
NOBLE = {"distribution": "ubuntu", "version": "24.04", "codename": "noble"}


def write_json(path: Path, value: object) -> Path:
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2), encoding="utf-8")
    return path


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def profile_data() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def appliance() -> clone_profile.Profile:
    return clone_profile.load_profile(PROFILE_PATH)


def sample_package() -> dict:
    """The consumer's sample package, as the profile names it."""
    check = next(entry for entry in profile_data()["checks"] if "where" in entry.get("items", {}))
    return {**check["items"]["where"], **check["items"]["each"]}


def evidence() -> dict:
    """Evidence that a genuine appliance clone writes."""
    return {
        KNOWN_ANSWER: {"schema": "projectbuddy/labview-known-answer-capture@1", "inputs": [20, 22],
                       "expectedOutput": 42, "exitCode": 0, "wallMs": 5,
                       "output": "Operation output:\n42\nRunVI operation succeeded.\n",
                       "host": {"os": "linux", "hostname": "h", "bootId": BOOT}},
        RUNTIME: {"bootId": BOOT, "hostname": "h", "os": dict(NOBLE), "automaticLoginEnabled": False,
                  "labviewPackageVersion": "26.3.1.49165-0+f13"},
        ABOUT: {"version": "26.3.2", "build_number": "4056", "edition": "Professional",
                "valid_through": "Perpetual", "valid_activation_code": True},
        TARGETS: {"labview_versions": [{"version_year": "2026", "bitness": 64, "vi_server_tcp_port": 3363}]},
        INSTALLED: {"dependencies": [{"name": "other", "installed_version": "1"}, sample_package()]},
    }


def set_pointer(document: dict, pointer: str, value: object) -> None:
    *parents, last = pointer.split("/")[1:]
    for token in parents:
        document = document[token]
    if value is DELETE:
        del document[last]
    else:
        document[last] = value


DELETE = object()


def profile_copy(directory: Path, drop: tuple[str, ...] = (), **changes) -> clone_profile.Profile:
    data = profile_data()
    for field in drop:
        data.pop(field)
    data.update(changes)
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(APPLIANCE / data["capture"]["script"], directory)
    return clone_profile.load_profile(write_json(directory / "clone-profile.json", data))


def minimal(**changes) -> dict:
    data = {
        "schema": clone_profile.PROFILE_SCHEMA, "name": "minimal",
        "capture": {"script": "capture.sh", "timeoutSeconds": 60, "files": ["a.json"]},
        "bootId": {"file": "a.json", "pointer": "/boot", "alsoIn": [{"file": "a.json", "pointer": "/host/boot"}]},
        "checks": [{"file": "a.json", "pointer": "/value", "equals": 1, "message": "value"}],
        "runnerUnits": "record",
    }
    data.update(changes)
    return data


def parse(data: object) -> clone_profile.Profile:
    return clone_profile.parse_profile(data, Path("/profiles"), "0" * 64)


def replace_check(**test):
    def apply(data: dict) -> None:
        data["checks"][0] = {"file": "a.json", "pointer": "/value", "message": "value", **test}
    return apply


# The clone profile.

def test_the_appliance_profile_loads():
    loaded = appliance()
    assert loaded.name == "ubuntu-labview-appliance"
    assert loaded.runner_units == "record"
    assert loaded.script == "capture-activation.sh" and (APPLIANCE / loaded.script).is_file()
    assert loaded.timeout == 600
    assert loaded.files == (KNOWN_ANSWER, RUNTIME, ABOUT, TARGETS, INSTALLED)
    assert loaded.boot_id == ((RUNTIME, "/bootId"), (KNOWN_ANSWER, "/host/bootId"))
    assert len(loaded.checks) == 14
    assert loaded.receipt_v1_runtime == profile_data()["receiptV1Runtime"]
    assert loaded.directory == APPLIANCE and loaded.sha256 == sha256_file(PROFILE_PATH)


def test_the_v1_pin_is_the_appliance_profile(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert runpy.run_path(str(RECEIPT_ADAPTER), run_name="adapter")["V1_PROFILE_SHA256"] == sha256_file(PROFILE_PATH)


def test_the_appliance_profile_matches_the_manifest():
    manifest = json.loads((APPLIANCE / "provisioning-manifest.json").read_text(encoding="utf-8"))
    data = profile_data()
    checks = {(entry["file"], entry["pointer"]): entry for entry in data["checks"]}
    os_fields = {field: manifest["os"][field] for field in NOBLE}
    version, build = manifest["vipm"]["version"].split("-")
    assert checks[(RUNTIME, "/os")]["equals"] == os_fields
    assert checks[(RUNTIME, "/labviewPackageVersion")]["equals"] == manifest["labview"]["packageVersion"]
    target = checks[(TARGETS, "/labview_versions")]["items"]["each"]
    assert target["version_year"] == manifest["labview"]["year"]
    assert target["vi_server_tcp_port"] == manifest["labview"]["viServerPort"]
    assert checks[(ABOUT, "/version")]["equals"] == version
    assert checks[(ABOUT, "/build_number")]["oneOf"] == [build, int(build)]
    runtime = data["receiptV1Runtime"]
    assert runtime["os"] == os_fields
    assert runtime["labviewPackageVersion"] == manifest["labview"]["packageVersion"]
    assert runtime["knownAnswer"]["viServerPort"] == manifest["labview"]["viServerPort"]
    assert (runtime["vipm"]["version"], runtime["vipm"]["buildNumber"]) == (version, build)


def test_a_minimal_profile_parses():
    loaded = parse(minimal())
    assert loaded.boot_id == (("a.json", "/boot"), ("a.json", "/host/boot"))
    assert loaded.checks == (clone_profile.Check("a.json", "/value", "value", "equals", 1),)
    assert loaded.receipt_v1_runtime is None and loaded.sha256 == "0" * 64


@pytest.mark.parametrize("change", [
    lambda data: data["capture"].update(timeoutSeconds=1),
    lambda data: data["capture"].update(timeoutSeconds=86400),
    lambda data: data["bootId"].update(pointer=""),
    lambda data: data["bootId"].update(pointer="/a~0b~1c/0"),
    replace_check(items={"count": 0}),
    replace_check(items={"count": 1, "where": {}, "each": {}}),
    replace_check(matches=".*"),
    replace_check(oneOf=[None]),
    lambda data: data.update(runnerUnits="refuse", receiptV1Runtime={}),
])
def test_a_profile_accepts_its_boundaries(change):
    data = minimal()
    change(data)
    parse(data)


PROFILE_REFUSALS = [
    (lambda data: data.pop("checks"), "clone profile is missing checks"),
    (lambda data: data.update(extra=1), "clone profile has unknown fields extra"),
    (lambda data: data.update(schema="projectbuddy/ubuntu-vagrant-clone-profile@2"), "schema must be"),
    (lambda data: data.update(name="Minimal"), "name must match"),
    (lambda data: data.update(name=1), "name must match"),
    (lambda data: data.update(capture=[]), "capture must be an object"),
    (lambda data: data["capture"].update(extra=1), "capture has unknown fields extra"),
    (lambda data: data["capture"].update(script="../capture.sh"), "capture.script must be"),
    (lambda data: data["capture"].update(script=""), "capture.script must be"),
    (lambda data: data["capture"].update(timeoutSeconds=0), "capture.timeoutSeconds must be"),
    (lambda data: data["capture"].update(timeoutSeconds=86401), "capture.timeoutSeconds must be"),
    (lambda data: data["capture"].update(timeoutSeconds=True), "capture.timeoutSeconds must be"),
    (lambda data: data["capture"].update(timeoutSeconds=60.0), "capture.timeoutSeconds must be"),
    (lambda data: data["capture"].update(files=[]), "capture.files must be"),
    (lambda data: data["capture"].update(files="a.json"), "capture.files must be"),
    (lambda data: data["capture"].update(files=["a.txt"]), "capture.files must be"),
    (lambda data: data["capture"].update(files=["a.json", "a.json"]), "capture.files must be"),
    (lambda data: data["capture"].update(files=[1]), "capture.files must be"),
    (lambda data: data.update(bootId=[]), "bootId must be an object"),
    (lambda data: data["bootId"].update(extra=1), "bootId has unknown fields extra"),
    (lambda data: data["bootId"].update(file="b.json"), "bootId.file must be one of capture.files"),
    (lambda data: data["bootId"].update(pointer="boot"), "bootId.pointer must be a JSON pointer"),
    (lambda data: data["bootId"].update(pointer="/~2"), "bootId.pointer must be a JSON pointer"),
    (lambda data: data["bootId"].update(pointer=1), "bootId.pointer must be a JSON pointer"),
    (lambda data: data["bootId"].update(alsoIn=[]), "bootId.alsoIn must be a non-empty list"),
    (lambda data: data["bootId"].update(alsoIn={}), "bootId.alsoIn must be a non-empty list"),
    (lambda data: data["bootId"].update(alsoIn=["a.json"]), "bootId.alsoIn[0] must be an object"),
    (lambda data: data["bootId"]["alsoIn"][0].update(extra=1), "bootId.alsoIn[0] has unknown fields extra"),
    (lambda data: data["bootId"]["alsoIn"][0].update(pointer="x"), "bootId.alsoIn[0].pointer must be"),
    (lambda data: data["bootId"]["alsoIn"][0].update(file="b.json"), "bootId.alsoIn[0].file must be"),
    (lambda data: data.update(checks=[]), "checks must be a non-empty list"),
    (lambda data: data.update(checks={}), "checks must be a non-empty list"),
    (lambda data: data["checks"].append("x"), "checks[1] must have exactly one of equals, oneOf, matches, items"),
    (lambda data: data["checks"][0].update(oneOf=[1]), "checks[0] must have exactly one of"),
    (lambda data: data["checks"][0].pop("equals"), "checks[0] must have exactly one of"),
    (lambda data: data["checks"][0].update(extra=1), "checks[0] has unknown fields extra"),
    (lambda data: data["checks"][0].pop("message"), "checks[0] is missing message"),
    (lambda data: data["checks"][0].update(message=" "), "checks[0].message must be a non-empty string"),
    (lambda data: data["checks"][0].update(message=1), "checks[0].message must be a non-empty string"),
    (lambda data: data["checks"][0].update(file="b.json"), "checks[0].file must be one of capture.files"),
    (lambda data: data["checks"][0].update(pointer="value"), "checks[0].pointer must be a JSON pointer"),
    (replace_check(oneOf=[]), "checks[0].oneOf must be a non-empty list"),
    (replace_check(oneOf="x"), "checks[0].oneOf must be a non-empty list"),
    (replace_check(matches="("), "checks[0].matches must be a regular expression"),
    (replace_check(matches=1), "checks[0].matches must be a regular expression"),
    (replace_check(items=[]), "checks[0].items must be an object"),
    (replace_check(items={}), "checks[0].items is missing count"),
    (replace_check(items={"count": 1, "extra": {}}), "checks[0].items has unknown fields extra"),
    (replace_check(items={"count": -1}), "checks[0].items.count must be a non-negative integer"),
    (replace_check(items={"count": True}), "checks[0].items.count must be a non-negative integer"),
    (replace_check(items={"count": 1, "where": []}), "checks[0].items.where must be an object"),
    (replace_check(items={"count": 1, "each": 1}), "checks[0].items.each must be an object"),
    (lambda data: data.update(runnerUnits="allow"), "runnerUnits must be refuse or record"),
    (lambda data: data.pop("runnerUnits"), "runnerUnits must be refuse or record"),
    (lambda data: data.update(receiptV1Runtime=[]), "receiptV1Runtime must be an object"),
    (lambda data: data.update(receiptV1Runtime=None), "receiptV1Runtime must be an object"),
]


@pytest.mark.parametrize("change, message", PROFILE_REFUSALS)
def test_a_profile_is_refused(change, message):
    data = minimal()
    change(data)
    with pytest.raises(ProfileError, match=re.escape(message)):
        parse(data)


def test_a_profile_names_every_error():
    with pytest.raises(ProfileError) as raised:
        parse(minimal(name="X", runnerUnits="allow"))
    assert str(raised.value) == "name must match [a-z0-9][a-z0-9._+-]*; runnerUnits must be refuse or record"


def test_a_profile_must_be_an_object():
    with pytest.raises(ProfileError, match="clone profile must be a JSON object"):
        parse([])


@pytest.mark.parametrize("content", [
    b'{"schema": 1, "schema": 2}', b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}', b"\xff", b"{"])
def test_an_unreadable_profile_is_refused(tmp_path, content):
    path = tmp_path / "clone-profile.json"
    path.write_bytes(content)
    with pytest.raises(ProfileError, match="clone profile is unreadable"):
        clone_profile.load_profile(path)
    with pytest.raises(ProfileError, match="clone profile is unreadable"):
        clone_profile.load_profile(tmp_path / "missing.json")


# Strict JSON, typed equality, and pointers.

@pytest.mark.parametrize("text, message", [
    ('{"a": 1, "a": 1}', "the key 'a' appears twice"),
    ('{"a": {"b": 1, "b": 2}}', "the key 'b' appears twice"),
    ('[NaN]', "NaN is not JSON"),
    ('{"a": Infinity}', "Infinity is not JSON"),
])
def test_parse_json_is_strict(text, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        clone_profile.parse_json(text)
    assert clone_profile.parse_json('{"a": [1, 1.5, true, null]}') == {"a": [1, 1.5, True, None]}


@pytest.mark.parametrize("left, right, equal", [
    (1, 1, True), (1, True, False), (True, 1, False), (1, 1.0, False), (0, False, False), (None, None, True),
    ("1", 1, False), ({"a": [1, "x"]}, {"a": [1, "x"]}, True), ({"a": [1]}, {"a": [True]}, False),
    ({"a": 1}, {"a": 1, "b": 2}, False), ({"a": 1}, {"b": 1}, False), ([1, 2], [1, 2, 3], False),
    ([1, 2], [2, 1], False), ({}, [], False),
])
def test_same_compares_by_json_type(left, right, equal):
    assert clone_profile.same(left, right) is equal
    assert clone_profile.same(right, left) is equal


DOCUMENT = {"a": {"b": [10, {"c": 3}]}, "x/y": 1, "m~n": 2, "~1": 3, "": 4}


@pytest.mark.parametrize("pointer, value", [
    ("", DOCUMENT), ("/a/b/0", 10), ("/a/b/1/c", 3), ("/x~1y", 1), ("/m~0n", 2), ("/~01", 3), ("/", 4)])
def test_resolve_follows_rfc_6901(pointer, value):
    assert clone_profile.resolve(DOCUMENT, pointer) == value


@pytest.mark.parametrize("pointer", ["/missing", "/a/b/2", "/a/b/01", "/a/b/-", "/a/b/x", "/a/b/0/c", "/x/y"])
def test_resolve_reports_an_unresolved_pointer(pointer):
    assert clone_profile.resolve(DOCUMENT, pointer) is clone_profile._UNRESOLVED


# The four tests.

@pytest.mark.parametrize("test, value, passes", [
    ({"equals": 1}, 1, True),
    ({"equals": 1}, True, False),
    ({"equals": 1}, 1.0, False),
    ({"equals": None}, None, True),
    ({"equals": {"a": [1]}}, {"a": [1]}, True),
    ({"equals": {"a": [1]}}, {"a": [True]}, False),
    ({"oneOf": ["4056", 4056]}, 4056, True),
    ({"oneOf": ["4056", 4056]}, "4056", True),
    ({"oneOf": ["4056", 4056]}, 4056.0, False),
    ({"oneOf": [1]}, True, False),
    ({"matches": "b+"}, "abbc", True),
    ({"matches": "^b"}, "abc", False),
    ({"matches": "1"}, 1, False),
    ({"items": {"count": 2}}, [1, "x"], True),
    ({"items": {"count": 2}}, [1], False),
    ({"items": {"count": 1}}, {"a": 1}, False),
    ({"items": {"count": 0, "where": {"a": 1}}}, [], True),
    ({"items": {"count": 1, "where": {"a": 1}}}, [{"a": 1}, {"a": 2}], True),
    ({"items": {"count": 1, "where": {"a": 1}}}, [{"a": 1}, {"a": True}], True),
    ({"items": {"count": 1, "where": {"a": 1}}}, [{"a": 1}, {"a": 1}], False),
    ({"items": {"count": 1, "where": {"a": 1}}}, [{"b": 1}], False),
    ({"items": {"count": 1, "where": {"a": 1}}}, [{"a": 1}, 1], False),
    ({"items": {"count": 1, "each": {"b": 2}}}, [{"b": 2, "c": 3}], True),
    ({"items": {"count": 1, "each": {"b": 2}}}, [{"b": 3}], False),
    ({"items": {"count": 1, "each": {"b": 2}}}, [{}], False),
    ({"items": {"count": 1, "each": {"b": 2}}}, ["b"], False),
    ({"items": {"count": 1, "where": {"a": 1}, "each": {"b": 2}}}, [{"a": 1, "b": 2}, {"a": 2}], True),
    ({"items": {"count": 1, "where": {"a": 1}, "each": {"b": 2}}}, [{"a": 1, "b": 3}, {"a": 2, "b": 2}], False),
])
def test_each_test_kind(test, value, passes):
    data = minimal()
    replace_check(**test)(data)
    assert clone_profile.failures(parse(data), {"a.json": {"value": value}}) == ([] if passes else ["value"])


@pytest.mark.parametrize("test", [{"equals": None}, {"oneOf": [None]}, {"matches": ""}, {"items": {"count": 0}}])
def test_an_unresolved_pointer_fails_every_test(test):
    data = minimal()
    replace_check(**test)(data)
    assert clone_profile.failures(parse(data), {"a.json": {}}) == ["value"]


def test_failures_report_each_message_once_in_profile_order():
    data = minimal(checks=[{"file": "a.json", "pointer": pointer, "equals": 1, "message": message}
                           for pointer, message in (("/a", "first"), ("/b", "second"), ("/c", "first"))])
    loaded = parse(data)
    assert clone_profile.failures(loaded, {"a.json": {"a": 2, "b": 2, "c": 2}}) == ["first", "second"]
    assert clone_profile.failures(loaded, {"a.json": {"a": 1, "b": 2, "c": 2}}) == ["second", "first"]
    assert clone_profile.failures(loaded, {"a.json": {"a": 1, "b": 1, "c": 1}}) == []


# The appliance profile against its evidence.

def test_genuine_appliance_evidence_passes():
    assert clone_profile.failures(appliance(), evidence()) == []
    assert clone_profile.boot_id(appliance(), evidence()) == BOOT


def test_an_integer_build_number_passes_as_before():
    files = evidence()
    files[ABOUT]["build_number"] = 4056
    assert clone_profile.failures(appliance(), files) == []


KNOWN_ANSWER_FAILED = "clone LabVIEW known-answer probe failed"
SECURITY_FAILED = "clone LabVIEW package or security state is invalid"
VIPM_FAILED = "clone VIPM activation state is invalid"
TARGET_FAILED = "clone LabVIEW target or VI Server contract is invalid"
PACKAGE_FAILED = "clone sample package is not installed at the expected version"
TARGET = {"version_year": "2026", "bitness": 64, "vi_server_tcp_port": 3363}


@pytest.mark.parametrize("file, pointer, value, message", [
    (KNOWN_ANSWER, "/exitCode", False, KNOWN_ANSWER_FAILED),
    (KNOWN_ANSWER, "/exitCode", 1, KNOWN_ANSWER_FAILED),
    (KNOWN_ANSWER, "/inputs", [20.0, 22.0], KNOWN_ANSWER_FAILED),
    (KNOWN_ANSWER, "/expectedOutput", 41, KNOWN_ANSWER_FAILED),
    (KNOWN_ANSWER, "/output", "Operation output: 41\nRunVI operation succeeded.", KNOWN_ANSWER_FAILED),
    (KNOWN_ANSWER, "/output", DELETE, KNOWN_ANSWER_FAILED),
    (RUNTIME, "/os", {**NOBLE, "codename": "jammy"}, "clone guest OS does not match the appliance contract"),
    (RUNTIME, "/os", {**NOBLE, "extra": 1}, "clone guest OS does not match the appliance contract"),
    (RUNTIME, "/labviewPackageVersion", "26.3.0", SECURITY_FAILED),
    (RUNTIME, "/automaticLoginEnabled", 0, SECURITY_FAILED),
    (ABOUT, "/version", "26.3.3", VIPM_FAILED),
    (ABOUT, "/build_number", "4057", VIPM_FAILED),
    (ABOUT, "/edition", "Community", VIPM_FAILED),
    (ABOUT, "/valid_through", "2027-01-01", VIPM_FAILED),
    (ABOUT, "/valid_activation_code", 1, VIPM_FAILED),
    (TARGETS, "/labview_versions", [TARGET, TARGET], TARGET_FAILED),
    (TARGETS, "/labview_versions", [{**TARGET, "bitness": 32}], TARGET_FAILED),
    (TARGETS, "/labview_versions", [{**TARGET, "vi_server_tcp_port": 3364}], TARGET_FAILED),
    (INSTALLED, "/dependencies", [{"name": "other"}], PACKAGE_FAILED),
    (INSTALLED, "/dependencies", "none", PACKAGE_FAILED),
])
def test_appliance_evidence_fails_its_check(file, pointer, value, message):
    files = evidence()
    set_pointer(files[file], pointer, value)
    assert clone_profile.failures(appliance(), files) == [message]


@pytest.mark.parametrize("field, value", [("installed_version", "2.0.0.20"), ("status", "missing")])
def test_the_sample_package_must_be_installed_at_its_version(field, value):
    files = evidence()
    files[INSTALLED]["dependencies"][1][field] = value
    assert clone_profile.failures(appliance(), files) == [PACKAGE_FAILED]


# The boot ID.

def test_the_boot_id_is_canonical_and_shared():
    files = evidence()
    files[RUNTIME]["bootId"] = BOOT.upper()
    assert clone_profile.boot_id(appliance(), files) == BOOT
    files[KNOWN_ANSWER]["host"]["bootId"] = BOOT.upper()
    with pytest.raises(ProfileError, match="clone captures do not share one boot ID"):
        clone_profile.boot_id(appliance(), files)


@pytest.mark.parametrize("value", [None, 1, "not-a-uuid", DELETE])
def test_a_boot_id_that_is_not_a_uuid_is_refused(value):
    files = evidence()
    set_pointer(files[RUNTIME], "/bootId", value)
    with pytest.raises(ProfileError, match=f"the boot ID in {RUNTIME} is not a UUID"):
        clone_profile.boot_id(appliance(), files)


@pytest.mark.parametrize("value", [SOURCE_UUID, DELETE, None])
def test_a_second_boot_id_must_match(value):
    files = evidence()
    set_pointer(files[KNOWN_ANSWER], "/host/bootId", value)
    with pytest.raises(ProfileError, match="clone captures do not share one boot ID"):
        clone_profile.boot_id(appliance(), files)


@pytest.mark.parametrize("content", [None, '{"version": 1, "version": 1}', "{", b"\xff"])
def test_unreadable_evidence_is_refused(tmp_path, content):
    for name, value in evidence().items():
        write_json(tmp_path / name, value)
    if content is None:
        (tmp_path / ABOUT).unlink()
    elif isinstance(content, bytes):
        (tmp_path / ABOUT).write_bytes(content)
    else:
        (tmp_path / ABOUT).write_text(content, encoding="utf-8")
    with pytest.raises(ProfileError, match=f"clone evidence {re.escape(ABOUT)} is unreadable"):
        clone_profile.load_evidence(appliance(), tmp_path)


def test_evidence_loads_by_file_name(tmp_path):
    for name, value in evidence().items():
        write_json(tmp_path / name, value)
    assert clone_profile.load_evidence(appliance(), tmp_path) == evidence()


# Neutral core: the request, the machine ID, the instance, and runner units.

@pytest.fixture
def request_paths(tmp_path):
    password = tmp_path / "password"
    password.write_text("secret\n", encoding="utf-8")
    password.chmod(0o600)
    return SimpleNamespace(password=password, evidence=private_dir(tmp_path / "evidence"))


def test_a_request_returns_the_canonical_uuid(request_paths):
    for port in (1024, PORT, 65535):
        assert core.check_request(NAME, UUID.upper(), port, request_paths.password, request_paths.evidence) == UUID
    request_paths.password.chmod(0o400)
    assert core.check_request(NAME, UUID, PORT, request_paths.password, request_paths.evidence) == UUID


@pytest.mark.parametrize("change, message", [
    (dict(uuid="clone-1"), "clone VM UUID is not a UUID"),
    (dict(name=""), "VM name is invalid"),
    (dict(name="clone 1"), "VM name is invalid"),
    (dict(name="clone/1"), "VM name is invalid"),
    (dict(ssh_port=1023), "SSH port must be from 1024 through 65535"),
    (dict(ssh_port=65536), "SSH port must be from 1024 through 65535"),
    (dict(ssh_port=True), "SSH port must be from 1024 through 65535"),
    (dict(ssh_port=2222.0), "SSH port must be from 1024 through 65535"),
    (dict(ssh_port="2222"), "SSH port must be from 1024 through 65535"),
    (dict(password_file=Path("password")), "guest password file must be an existing absolute path"),
    (dict(password_file="missing"), "guest password file must be an existing absolute path"),
    (dict(password_file="directory"), "guest password file must be an existing absolute path"),
    (dict(password_mode=0o640), "guest password file must have mode 600 or stricter"),
    (dict(password_mode=0o604), "guest password file must have mode 600 or stricter"),
    (dict(evidence_dir=Path("evidence")), "evidence directory must be an existing absolute path"),
    (dict(evidence_dir="missing"), "evidence directory must be an existing absolute path"),
    (dict(evidence_mode=0o750), "evidence directory must have mode 700 or stricter"),
])
def test_a_request_is_refused(request_paths, monkeypatch, change, message):
    monkeypatch.chdir(request_paths.evidence.parent)
    arguments = dict(name=NAME, uuid=UUID, ssh_port=PORT, password_file=request_paths.password,
                     evidence_dir=request_paths.evidence)
    password_mode = change.pop("password_mode", None)
    evidence_mode = change.pop("evidence_mode", None)
    for key in ("password_file", "evidence_dir"):
        if change.get(key) == "missing":
            change[key] = request_paths.evidence.parent / "missing"
        elif change.get(key) == "directory":
            change[key] = request_paths.evidence
    arguments.update(change)
    if password_mode is not None:
        request_paths.password.chmod(password_mode)
    if evidence_mode is not None:
        request_paths.evidence.chmod(evidence_mode)
    with pytest.raises(LaneBoxError, match=re.escape(message)):
        core.check_request(**arguments)


def test_a_refused_request_is_a_clone_error(request_paths):
    with pytest.raises(CloneError):
        core.check_request(NAME, UUID, PORT, request_paths.password, Path("evidence"))


def test_the_machine_id_must_name_the_clone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "id"
    path.write_text(UUID.upper() + "\n", encoding="utf-8")
    core.check_machine_id(path, UUID)
    path.write_text(SOURCE_UUID, encoding="utf-8")
    with pytest.raises(CloneError, match="Vagrant machine ID does not match the expected clone UUID"):
        core.check_machine_id(path, UUID)
    path.write_text("clone-1", encoding="utf-8")
    with pytest.raises(LaneBoxError, match="Vagrant machine ID is not a UUID"):
        core.check_machine_id(path, UUID)
    path.write_bytes(b"\xff")
    with pytest.raises(CloneError, match="Vagrant machine ID file is unreadable"):
        core.check_machine_id(path, UUID)
    for refused in (Path("id"), tmp_path / "missing", tmp_path):
        with pytest.raises(CloneError, match="Vagrant machine ID file must be an existing absolute path"):
            core.check_machine_id(refused, UUID)


def test_vm_info_splits_on_the_first_equals_sign():
    output = f'name="{NAME}"\nUUID="{UUID}"\nno equals sign\nkey==value\nspaced= "x" \n'
    assert core.parse_vm_info(output) == {"name": NAME, "UUID": UUID, "key": "=value", "spaced": "x"}


SSH_RULE = f"ssh,tcp,127.0.0.1,{PORT},,22"


def instance(**changes) -> dict:
    info = {"UUID": UUID, "name": NAME, "VMState": "running", "Forwarding(0)": SSH_RULE}
    info.update(changes)
    return {key: value for key, value in info.items() if value is not None}


@pytest.mark.parametrize("info", [
    instance(),
    instance(**{"Forwarding(1)": "http,tcp,127.0.0.1,8080,,80"}),
    instance(**{"Forwarding(1)": "short,tcp,,22"}),
])
def test_an_instance_is_accepted(info):
    core.check_instance(info, NAME, UUID, PORT)


@pytest.mark.parametrize("info, message", [
    (instance(UUID=SOURCE_UUID), "clone VM UUID does not match expected identity"),
    (instance(UUID=None), "clone VM UUID does not match expected identity"),
    (instance(name="clone-2"), "clone VM name does not match expected identity"),
    (instance(VMState="paused"), "clone VM must be running"),
    (instance(VMState=None), "clone VM must be running"),
    (instance(**{"Forwarding(0)": None}), "exactly one loopback SSH forwarding rule"),
    (instance(**{"Forwarding(1)": SSH_RULE}), "exactly one loopback SSH forwarding rule"),
    (instance(**{"Forwarding(1)": "other,tcp,127.0.0.1,2223,,22"}), "exactly one loopback SSH forwarding rule"),
    (instance(**{"Forwarding(0)": f"ssh,tcp,,{PORT},,22"}), "exactly one loopback SSH forwarding rule"),
    (instance(**{"Forwarding(0)": f"ssh,tcp,0.0.0.0,{PORT},,22"}), "exactly one loopback SSH forwarding rule"),
    (instance(**{"Forwarding(0)": "ssh,tcp,127.0.0.1,2223,,22"}), "exactly one loopback SSH forwarding rule"),
    (instance(**{"Forwarding(0)": f"ssh,udp,127.0.0.1,{PORT},,22"}), "exactly one loopback SSH forwarding rule"),
    (instance(**{"Forwarding(0)": f"ssh,tcp,127.0.0.1,{PORT},10.0.2.15,22"}),
     "exactly one loopback SSH forwarding rule"),
])
def test_an_instance_is_refused(info, message):
    with pytest.raises(CloneError, match=re.escape(message)):
        core.check_instance(info, NAME, UUID, PORT)


UNIT_FILES = f"ssh.service enabled enabled\n{RUNNER} enabled enabled\nsysinit.target static -\n"
UNITS = f"{RUNNER} loaded active running GitHub Actions Runner\nssh.service loaded active running OpenSSH\n"


def test_runner_units_are_found_sorted_and_once():
    other = "actions.runner.org-repo.runner-0.service"
    assert core.runner_units([(0, UNIT_FILES), (0, UNITS + f"{other} loaded inactive dead x\n")]) == [other, RUNNER]
    assert core.runner_units([(0, "ssh.service enabled enabled\n"), (0, "\n  \nssh.service loaded\n")]) == []


@pytest.mark.parametrize("glyph", sorted(core.STATE_GLYPHS))
def test_a_state_glyph_is_tolerated(glyph):
    assert core.runner_units([(0, UNIT_FILES), (0, f"{glyph} {RUNNER} loaded failed failed x\n")]) == [RUNNER]


def test_the_queries_list_everything_without_a_pattern():
    assert [query[:2] for query in core.RUNNER_UNIT_QUERIES] == [
        ("systemctl", "list-unit-files"), ("systemctl", "list-units")]
    for query in core.RUNNER_UNIT_QUERIES:
        assert {"--no-pager", "--no-legend", "--plain", "--full"} <= set(query)
        assert not any("actions" in word for word in query)
    assert "--all" in core.RUNNER_UNIT_QUERIES[1]


@pytest.mark.parametrize("results, message", [
    ([(0, UNIT_FILES)], "every runner unit query must have a result"),
    ([(0, UNIT_FILES)] * 3, "every runner unit query must have a result"),
    ([(1, UNIT_FILES), (0, UNITS)], "systemctl list-unit-files failed with exit code 1"),
    ([(0, UNIT_FILES), (124, "")], "systemctl list-units failed with exit code 124"),
    ([(0, ""), (0, UNITS)], "systemctl list-unit-files listed no units"),
    ([(0, UNIT_FILES), (0, " \n\n")], "systemctl list-units listed no units"),
    ([(0, "0 unit files listed.\n"), (0, UNITS)], "systemctl list-unit-files printed a line that is not a unit"),
    ([(0, "UNIT FILE STATE PRESET\n"), (0, UNITS)], "systemctl list-unit-files printed a line that is not a unit"),
    ([(0, UNIT_FILES), (0, "\u25cf\n")], "systemctl list-units printed a line that is not a unit"),
    ([(0, UNIT_FILES), (0, "Ssh.Service loaded\n")], "systemctl list-units printed a line that is not a unit"),
])
def test_runner_units_fail_closed(results, message):
    with pytest.raises(CloneError, match=re.escape(message)):
        core.runner_units(results)


# Receipts.

def write_evidence(directory: Path, files: dict | None = None) -> Path:
    private_dir(directory)
    for name, value in (files or evidence()).items():
        write_json(directory / name, value).chmod(0o600)
    return directory


def setup_receipt(tmp_path: Path, profile: clone_profile.Profile | None = None,
                  observed: tuple = ()) -> SimpleNamespace:
    loaded = profile or appliance()
    evidence_dir = write_evidence(tmp_path / "evidence")
    box_receipt = write_json(tmp_path / "box-receipt.json", {"schema": "projectbuddy/ubuntu-vagrant-box@1",
                                                             "box": {"sha256": BOX_SHA256}})
    receipt = clone_receipt.build_receipt(loaded, box=clone_receipt.source_box(box_receipt), name=NAME,
                                          uuid=UUID, boot=BOOT, evidence_dir=evidence_dir, observed=list(observed))
    path = write_json(evidence_dir / clone_receipt.RECEIPT_FILENAME, receipt)
    return SimpleNamespace(profile=loaded, evidence_dir=evidence_dir, box_receipt=box_receipt, path=path,
                           receipt=receipt)


def verify(setup: SimpleNamespace, **options) -> dict:
    options = {"name": NAME, "uuid": UUID, **options}
    return clone_receipt.verify_receipt(setup.path, profile=setup.profile, box_receipt=setup.box_receipt,
                                        evidence_dir=setup.evidence_dir, **options)


def rewrite(setup: SimpleNamespace, change) -> None:
    receipt = json.loads(setup.path.read_text(encoding="utf-8"))
    change(receipt)
    write_json(setup.path, receipt)


def test_the_box_receipt_gives_the_source_box(tmp_path):
    path = write_json(tmp_path / "box-receipt.json", {"box": {"sha256": BOX_SHA256}})
    assert clone_receipt.source_box(path) == {"receiptSha256": sha256_file(path), "boxSha256": BOX_SHA256}


@pytest.mark.parametrize("content, message", [
    ({}, "box receipt does not record a box SHA-256"),
    ({"box": []}, "box receipt does not record a box SHA-256"),
    ({"box": {"sha256": "A" * 64}}, "box receipt does not record a box SHA-256"),
    ({"box": {"sha256": "a" * 63}}, "box receipt does not record a box SHA-256"),
    ({"box": {"sha256": 1}}, "box receipt does not record a box SHA-256"),
    ([], "box receipt must be a JSON object"),
    ('{"box": 1, "box": 2}', "box receipt is unreadable: the key 'box' appears twice"),
    ("{", "box receipt is unreadable"),
    (None, "box receipt is unreadable"),
])
def test_a_box_receipt_is_refused(tmp_path, content, message):
    path = tmp_path / "box-receipt.json"
    if content is not None:
        write_json(path, content)
    with pytest.raises(ReceiptError, match=re.escape(message)):
        clone_receipt.source_box(path)


def test_a_receipt_records_its_inputs(tmp_path):
    setup = setup_receipt(tmp_path)
    assert setup.receipt == {
        "schema": "projectbuddy/ubuntu-vagrant-clone@2",
        "profile": {"name": "ubuntu-labview-appliance", "sha256": sha256_file(PROFILE_PATH)},
        "sourceBox": {"receiptSha256": sha256_file(setup.box_receipt), "boxSha256": BOX_SHA256},
        "clone": {"vm": {"name": NAME, "uuid": UUID}, "bootId": BOOT, "finalState": "poweroff"},
        "runnerUnits": {"policy": "record", "observed": []},
        "evidenceSha256": {name: sha256_file(setup.evidence_dir / name) for name in evidence()},
        "failures": [],
        "outcome": "PASS",
    }
    assert verify(setup) == setup.receipt
    assert verify(setup, uuid=UUID.upper()) == setup.receipt


def test_a_receipt_with_recorded_runner_units_verifies(tmp_path):
    setup = setup_receipt(tmp_path, observed=("actions.runner.a.service", RUNNER))
    assert verify(setup)["runnerUnits"]["observed"] == ["actions.runner.a.service", RUNNER]


@pytest.mark.parametrize("change, message", [
    (lambda r: r["profile"].update(name="other"), "receipt does not match its inputs: profile"),
    (lambda r: r["profile"].update(sha256="0" * 64), "receipt does not match its inputs: profile"),
    (lambda r: r["sourceBox"].update(boxSha256="b" * 64), "receipt does not match its inputs: sourceBox"),
    (lambda r: r["sourceBox"].update(receiptSha256="b" * 64), "receipt does not match its inputs: sourceBox"),
    (lambda r: r["clone"]["vm"].update(name="clone-2"), "receipt does not match its inputs: clone"),
    (lambda r: r["clone"]["vm"].update(uuid=UUID.upper()), "receipt does not match its inputs: clone"),
    (lambda r: r["clone"].update(bootId=SOURCE_UUID), "receipt does not match its inputs: clone"),
    (lambda r: r["clone"].update(finalState="running"), "receipt does not match its inputs: clone"),
    (lambda r: r["clone"].pop("finalState"), "receipt does not match its inputs: clone"),
    (lambda r: r["runnerUnits"].update(policy="refuse"), "receipt does not match its inputs: runnerUnits"),
    (lambda r: r["runnerUnits"].update(extra=1), "receipt does not match its inputs: runnerUnits"),
    (lambda r: r["evidenceSha256"].update({ABOUT: "0" * 64}), "receipt does not match its inputs: evidenceSha256"),
    (lambda r: r["evidenceSha256"].update(extra="0" * 64), "receipt does not match its inputs: evidenceSha256"),
    (lambda r: r["evidenceSha256"].pop(ABOUT), "receipt does not match its inputs: evidenceSha256"),
    (lambda r: r.update(failures=["x"]), "receipt does not match its inputs: failures"),
    (lambda r: r.update(outcome="FAIL"), "receipt does not match its inputs: outcome"),
    (lambda r: r.pop("outcome"), "receipt does not match its inputs: outcome"),
    (lambda r: r.update(note=1, failures=None), "receipt does not match its inputs: failures, note"),
    (lambda r: r.update(token="x"), "forbidden fields present: token; receipt does not match its inputs: token"),
    (lambda r: r["clone"]["vm"].update(Password="x"),
     "forbidden fields present: password; receipt does not match its inputs: clone"),
    (lambda r: r["runnerUnits"].update(observed="none"), "runner units must be a sorted list"),
    (lambda r: r.update(runnerUnits=[]), "runner units must be a sorted list"),
    (lambda r: r.pop("runnerUnits"), "runner units must be a sorted list"),
    (lambda r: r["runnerUnits"].update(observed=[RUNNER, "actions.runner.a.service"]),
     "runner units must be a sorted list"),
    (lambda r: r["runnerUnits"].update(observed=[RUNNER, RUNNER]), "runner units must be a sorted list"),
    (lambda r: r["runnerUnits"].update(observed=["ssh.service"]), "runner units must be a sorted list"),
    (lambda r: r["runnerUnits"].update(observed=[1]), "runner units must be a sorted list"),
    (lambda r: r.update(schema="projectbuddy/ubuntu-vagrant-clone@3"), "unexpected schema"),
    (lambda r: r.pop("schema"), "unexpected schema"),
])
def test_a_tampered_receipt_fails(tmp_path, change, message):
    setup = setup_receipt(tmp_path)
    rewrite(setup, change)
    with pytest.raises(ReceiptError) as raised:
        verify(setup)
    assert str(raised.value).startswith(message)


@pytest.mark.parametrize("change, message", [
    (dict(name="clone-2"), "receipt does not match its inputs: clone"),
    (dict(uuid=SOURCE_UUID), "receipt does not match its inputs: clone"),
])
def test_a_receipt_names_its_clone(tmp_path, change, message):
    with pytest.raises(ReceiptError, match=f"^{re.escape(message)}$"):
        verify(setup_receipt(tmp_path), **change)


def test_a_receipt_needs_a_uuid(tmp_path):
    with pytest.raises(LaneBoxError, match="clone VM UUID is not a UUID"):
        verify(setup_receipt(tmp_path), uuid="clone-1")


@pytest.mark.parametrize("content, message", [
    ('{"outcome": "PASS", "outcome": "PASS"}', "clone receipt is unreadable: the key 'outcome' appears twice"),
    ('{"outcome": NaN}', "clone receipt is unreadable: NaN is not JSON"),
    ("[]", "clone receipt must be a JSON object"),
])
def test_an_unreadable_receipt_fails(tmp_path, content, message):
    setup = setup_receipt(tmp_path)
    setup.path.write_text(content, encoding="utf-8")
    with pytest.raises(ReceiptError, match=re.escape(message)):
        verify(setup)


def test_evidence_changed_after_the_receipt_fails(tmp_path):
    setup = setup_receipt(tmp_path)
    write_json(setup.evidence_dir / RUNTIME, json.dumps(evidence()[RUNTIME], indent=4))
    with pytest.raises(ReceiptError, match="^receipt does not match its inputs: evidenceSha256$"):
        verify(setup)
    about = evidence()[ABOUT]
    about["edition"] = "Community"
    write_json(setup.evidence_dir / ABOUT, about)
    with pytest.raises(ReceiptError) as raised:
        verify(setup)
    assert str(raised.value) == ("raw capture: clone VIPM activation state is invalid; "
                                 "receipt does not match its inputs: evidenceSha256")


def test_a_boot_id_mismatch_fails_the_receipt(tmp_path):
    setup = setup_receipt(tmp_path)
    probe = evidence()[KNOWN_ANSWER]
    probe["host"]["bootId"] = SOURCE_UUID
    write_json(setup.evidence_dir / KNOWN_ANSWER, probe)
    with pytest.raises(ReceiptError) as raised:
        verify(setup)
    assert str(raised.value) == ("clone captures do not share one boot ID; "
                                 "receipt does not match its inputs: clone, evidenceSha256")


def test_a_changed_box_receipt_fails(tmp_path):
    setup = setup_receipt(tmp_path)
    write_json(setup.box_receipt, {"box": {"sha256": BOX_SHA256}, "note": 1})
    with pytest.raises(ReceiptError, match="^receipt does not match its inputs: sourceBox$"):
        verify(setup)


def test_missing_evidence_fails_at_once(tmp_path):
    setup = setup_receipt(tmp_path)
    (setup.evidence_dir / TARGETS).unlink()
    with pytest.raises(ProfileError, match=f"clone evidence {re.escape(TARGETS)} is unreadable"):
        verify(setup)


def test_a_refusing_profile_verifies_a_receipt_without_units(tmp_path):
    refusing = profile_copy(tmp_path / "profile", runnerUnits="refuse")
    setup = setup_receipt(tmp_path, profile=refusing)
    assert verify(setup)["runnerUnits"] == {"policy": "refuse", "observed": []}


def test_a_refusing_profile_fails_a_receipt_with_units(tmp_path):
    refusing = profile_copy(tmp_path / "profile", runnerUnits="refuse")
    setup = setup_receipt(tmp_path, profile=refusing, observed=(RUNNER,))
    with pytest.raises(ReceiptError, match="^the profile refuses Actions runner units, but the receipt records some$"):
        verify(setup)


def v1_receipt(setup: SimpleNamespace) -> dict:
    """A projectbuddy/ubuntu-vagrant-clone@1 receipt as the earlier verifier wrote it."""
    package = sample_package()
    return {
        "schema": "projectbuddy/ubuntu-vagrant-clone@1",
        "sourceBox": {"receiptSha256": sha256_file(setup.box_receipt), "boxSha256": BOX_SHA256},
        "clone": {"vm": {"name": NAME, "uuid": UUID}, "bootId": BOOT, "finalState": "poweroff"},
        "evidenceSha256": {Path(name).stem: sha256_file(setup.evidence_dir / name) for name in evidence()},
        "runtime": {
            "os": dict(NOBLE), "automaticLoginEnabled": False, "labviewPackageVersion": "26.3.1.49165-0+f13",
            "knownAnswer": {"inputs": [20, 22], "actualOutput": 42, "viServerPort": 3363},
            "vipm": {"version": "26.3.2", "buildNumber": "4056", "edition": "Professional",
                     "validThrough": "Perpetual"},
            "package": {"name": package["name"], "version": package["installed_version"],
                        "status": package["status"]},
        },
        "failures": [],
        "outcome": "PASS",
    }


@pytest.fixture
def v1_setup(tmp_path):
    setup = setup_receipt(tmp_path)
    setup.receipt = v1_receipt(setup)
    write_json(setup.path, setup.receipt)
    return setup


def test_a_genuine_v1_receipt_verifies_against_its_pin(v1_setup):
    assert verify(v1_setup, v1_profile_sha256=sha256_file(PROFILE_PATH)) == v1_setup.receipt
    assert clone_receipt.build_v1_receipt(appliance(), box=clone_receipt.source_box(v1_setup.box_receipt),
                                          name=NAME, uuid=UUID, boot=BOOT,
                                          evidence_dir=v1_setup.evidence_dir) == v1_setup.receipt


def test_a_v1_receipt_with_an_integer_build_number_verifies(v1_setup):
    about = evidence()[ABOUT]
    about["build_number"] = 4056
    write_json(v1_setup.evidence_dir / ABOUT, about)
    rewrite(v1_setup, lambda r: r["evidenceSha256"].update(
        {"sandbox-vipm-about": sha256_file(v1_setup.evidence_dir / ABOUT)}))
    verify(v1_setup, v1_profile_sha256=sha256_file(PROFILE_PATH))


@pytest.mark.parametrize("pin", [None, "0" * 64])
def test_a_v1_receipt_needs_its_pinned_profile(v1_setup, pin):
    with pytest.raises(ReceiptError, match="^a projectbuddy/ubuntu-vagrant-clone@1 receipt verifies only against"):
        verify(v1_setup, v1_profile_sha256=pin)


def test_a_v1_receipt_needs_a_profile_with_a_v1_runtime(v1_setup, tmp_path):
    v1_setup.profile = profile_copy(tmp_path / "profile", drop=("receiptV1Runtime",))
    with pytest.raises(ReceiptError, match="verifies only against its pinned profile"):
        verify(v1_setup, v1_profile_sha256=v1_setup.profile.sha256)


@pytest.mark.parametrize("change", [
    lambda r: r["runtime"]["vipm"].update(buildNumber=4056),
    lambda r: r["runtime"].update(automaticLoginEnabled=0),
    lambda r: r["runtime"]["knownAnswer"].update(inputs=[20.0, 22]),
    lambda r: r["runtime"]["package"].update(version="2.0.0.20"),
    lambda r: r["runtime"].pop("package"),
])
def test_a_tampered_v1_runtime_fails(v1_setup, change):
    rewrite(v1_setup, change)
    with pytest.raises(ReceiptError, match="^receipt does not match its inputs: runtime$"):
        verify(v1_setup, v1_profile_sha256=sha256_file(PROFILE_PATH))


@pytest.mark.parametrize("change, field", [
    (lambda r: r["clone"].pop("finalState"), "clone"),
    (lambda r: r["evidenceSha256"].update({KNOWN_ANSWER: r["evidenceSha256"].pop("activation-known-answer-capture")}),
     "evidenceSha256"),
    (lambda r: r.update(runnerUnits={"policy": "record", "observed": []}), "runnerUnits"),
])
def test_a_v1_receipt_keeps_its_own_shape(v1_setup, change, field):
    rewrite(v1_setup, change)
    with pytest.raises(ReceiptError, match=f"^receipt does not match its inputs: {field}$"):
        verify(v1_setup, v1_profile_sha256=sha256_file(PROFILE_PATH))


# The live check: commands, outputs, and the instance.

def test_run_command_returns_the_exit_code_and_output(tmp_path):
    script = "import os, sys; print(os.environ.get('MARK'), sys.stdin.read()); sys.exit(3)"
    stdin = write_json(tmp_path / "input", "given")
    with stdin.open("r", encoding="utf-8") as stream:
        assert live.run_command([sys.executable, "-c", script], timeout=30, environment={"MARK": "m"},
                                stdin=stream) == (3, "m given\n")
    assert live.run_command([sys.executable, "-c", "import sys; print(sys.stdin.read() == '')"], timeout=30) == (
        0, "True\n")


def test_run_command_reports_a_timeout_and_a_missing_program(tmp_path):
    assert live.run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1) == (124, "")
    assert live.run_command([str(tmp_path / "missing")], timeout=30) == (127, "")


def test_outputs_must_not_exist_yet(tmp_path):
    loaded = profile_copy(tmp_path / "profile")
    directory = private_dir(tmp_path / "evidence")
    live.check_outputs(loaded, directory)
    for name in (clone_receipt.RECEIPT_FILENAME, f"{clone_receipt.RECEIPT_FILENAME}.sha256", ABOUT):
        (directory / name).write_text("", encoding="utf-8")
        with pytest.raises(CloneError, match="clone evidence output already exists"):
            live.check_outputs(loaded, directory)
        (directory / name).unlink()
    (directory / ABOUT).mkdir()
    with pytest.raises(CloneError, match="clone evidence output already exists"):
        live.check_outputs(loaded, directory)
    (loaded.directory / loaded.script).unlink()
    with pytest.raises(CloneError, match=f"clone capture script is missing: {loaded.script}"):
        live.check_outputs(loaded, directory)


# Never a real VBoxManage: a call that escapes the fakes fails with 127.
VBOX = "/nonexistent/VBoxManage"


def make_clone(tmp_path: Path, **changes) -> live.Clone:
    secret = tmp_path / "secret dir"
    secret.mkdir(exist_ok=True)
    password = secret / "pass'word"
    password.write_text("guest password\n", encoding="utf-8")
    password.chmod(0o600)
    fields = dict(name=NAME, uuid=UUID, ssh_port=PORT, fingerprint=FINGERPRINT, password_file=password,
                  evidence_dir=private_dir(tmp_path / "evidence"), vboxmanage=VBOX)
    fields.update(changes)
    return live.Clone(**fields)


def test_observe_instance_reads_the_vm(tmp_path):
    clone = make_clone(tmp_path)
    calls = []

    def run(info, code=0):
        def answer(argv, *, timeout):
            calls.append((argv, timeout))
            return code, "\n".join(f'{key}="{value}"' for key, value in info.items())
        return answer

    live.observe_instance(clone, run(instance()))
    assert calls == [([VBOX, "showvminfo", UUID, "--machinereadable"], 60)]
    with pytest.raises(CloneError, match="^cannot read clone VM identity$"):
        live.observe_instance(clone, run(instance(), code=1))
    with pytest.raises(CloneError, match="clone VM must be running"):
        live.observe_instance(clone, run(instance(VMState="saved")))


class Guest:
    """A fake clone behind ``run``: it answers each command the live check sends and records it."""

    def __init__(self, evidence_dir: Path, *, units: str = "ssh.service enabled enabled\n", fail=(),
                 files: dict | None = None, enroll_failures: int = 0, poweroff_works: bool = True,
                 acpi_works: bool = True, on=None):
        self.evidence_dir = evidence_dir
        self.units = units
        self.fail = set(fail)
        self.files = evidence() if files is None else files
        self.enroll_failures = enroll_failures
        self.poweroff_works = poweroff_works
        self.acpi_works = acpi_works
        self.on = on or {}
        self.state = "running"
        self.calls: list[str] = []
        self.records: list[SimpleNamespace] = []
        self.known_hosts_present: list[bool] = []
        self.askpass: list[str] = []
        self.password_input: list[str] = []

    def label(self, argv: list[str]) -> str:
        if argv[0] == VBOX:
            return argv[3] if argv[1] == "controlvm" else argv[1]
        if argv[0] == str(ENROLL):
            return "enroll"
        if argv[0] == "scp":
            return "upload" if argv[-1].startswith("actor@") else f"retrieve {Path(argv[-1]).name}"
        command = argv[-1]
        if command.startswith("systemctl "):
            return command.split()[1]
        if command.startswith("chmod 700 "):
            return "capture"
        if command.startswith("sudo "):
            return "poweroff"
        return "prepare" if "install -d" in command else "cleanup"

    def __call__(self, argv, *, timeout, environment=None, stdin=subprocess.DEVNULL):
        label = self.label(argv)
        self.calls.append(label)
        self.records.append(SimpleNamespace(label=label, argv=argv, timeout=timeout, environment=environment,
                                            stdin=stdin))
        if label in self.on:
            self.on[label](self)
        if environment is not None and not self.askpass:
            askpass = Path(environment["SSH_ASKPASS"])
            assert stat.S_IMODE(askpass.stat().st_mode) == 0o700
            self.askpass.append(subprocess.run([str(askpass)], capture_output=True, text=True, check=True).stdout)
        if label in self.fail:
            return 1, ""
        if label == "showvminfo":
            return 0, f'name="{NAME}"\nVMState="{self.state}"\n'
        if label == "acpipowerbutton":
            if self.acpi_works:
                self.state = "poweroff"
            return 0, ""
        if label == "enroll":
            out = Path(argv[argv.index("--out") + 1])
            self.known_hosts_present.append(out.exists())
            if self.enroll_failures:
                self.enroll_failures -= 1
                out.write_text("partial\n", encoding="utf-8")
                return 1, ""
            out.write_text("[127.0.0.1]:2222 ssh-ed25519 AAAA\n", encoding="utf-8")
            return 0, ""
        if label == "list-unit-files":
            return 0, self.units
        if label == "list-units":
            return 0, "ssh.service loaded active running OpenSSH\n"
        if label.startswith("retrieve "):
            name = label.split(" ", 1)[1]
            content = self.files[name]
            Path(argv[-1]).write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        if label == "poweroff":
            self.password_input.append(stdin.read())
            if self.poweroff_works:
                self.state = "poweroff"
        return 0, ""


@pytest.fixture
def live_setup(tmp_path, monkeypatch):
    runtime = private_dir(tmp_path / "runtime")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    clone = make_clone(tmp_path)
    box_receipt = write_json(tmp_path / "box-receipt.json", {"box": {"sha256": BOX_SHA256}})
    sleeps: list[float] = []

    def check(guest: Guest, profile: clone_profile.Profile | None = None, **options) -> Path:
        return live.run_clone_check(clone, profile or appliance(), clone_receipt.source_box(box_receipt),
                                    box_receipt, run=guest, sleep=sleeps.append, enroll_script=ENROLL, **options)

    return SimpleNamespace(clone=clone, box_receipt=box_receipt, sleeps=sleeps, runtime=runtime, check=check,
                           guest=lambda **options: Guest(clone.evidence_dir, **options))


RETRIEVALS = [f"retrieve {name}" for name in evidence()]
PASSING_CALLS = ["enroll", "list-unit-files", "list-units", "prepare", "upload", "capture", *RETRIEVALS, "cleanup",
                 "poweroff", "showvminfo"]


def test_a_genuine_clone_passes(live_setup):
    guest = live_setup.guest()
    receipt_path = live_setup.check(guest)
    clone = live_setup.clone
    assert receipt_path == clone.evidence_dir / clone_receipt.RECEIPT_FILENAME
    assert guest.calls == PASSING_CALLS
    assert live_setup.sleeps == []
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt == clone_receipt.build_receipt(
        appliance(), box=clone_receipt.source_box(live_setup.box_receipt), name=NAME, uuid=UUID, boot=BOOT,
        evidence_dir=clone.evidence_dir, observed=[])
    verify_hash_file(clone.evidence_dir / f"{clone_receipt.RECEIPT_FILENAME}.sha256", receipt_path)
    for path in clone.evidence_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    assert sorted(path.name for path in clone.evidence_dir.iterdir()) == sorted(
        [*evidence(), clone_receipt.RECEIPT_FILENAME, f"{clone_receipt.RECEIPT_FILENAME}.sha256"])
    assert list(live_setup.runtime.iterdir()) == []
    assert guest.askpass == ["guest password\n"]
    assert guest.password_input == ["guest password\n"]
    assert guest.known_hosts_present == [False]


def test_the_live_check_sends_these_commands(live_setup):
    guest = live_setup.guest()
    live_setup.check(guest)
    by_label = {record.label: record for record in guest.records}
    ssh = by_label["prepare"].argv[:-1]
    known_hosts = by_label["enroll"].argv[by_label["enroll"].argv.index("--out") + 1]
    assert Path(known_hosts).parent.name.startswith(live.TEMPORARY_PREFIX)
    assert Path(known_hosts).parent.parent == live_setup.runtime
    assert ssh == ["ssh", "-p", str(PORT), "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                   "-o", "ConnectTimeout=10", "actor@127.0.0.1"]
    assert by_label["enroll"].argv == [str(ENROLL), "--host", "127.0.0.1", "--port", str(PORT),
                                       "--expected-fingerprint", FINGERPRINT, "--out", known_hosts]
    scratch = "/home/actor/projectbuddy-clone-proof"
    assert by_label["list-unit-files"].argv[-1] == " ".join(core.RUNNER_UNIT_QUERIES[0])
    assert by_label["list-units"].argv[-1] == " ".join(core.RUNNER_UNIT_QUERIES[1])
    assert by_label["prepare"].argv[-1] == f"rm -rf {scratch}; install -d -m 700 {scratch}"
    assert by_label["upload"].argv == ["scp", "-P", str(PORT), *ssh[3:-1],
                                       str(APPLIANCE / "capture-activation.sh"),
                                       f"actor@127.0.0.1:{scratch}/capture-activation.sh"]
    assert by_label["capture"].argv[-1] == (f"chmod 700 {scratch}/capture-activation.sh && "
                                            f"{scratch}/capture-activation.sh --output-dir {scratch}/evidence")
    assert by_label[f"retrieve {ABOUT}"].argv[-2:] == [f"actor@127.0.0.1:{scratch}/evidence/{ABOUT}",
                                                     str(live_setup.clone.evidence_dir / ABOUT)]
    assert by_label["cleanup"].argv[-1] == f"rm -rf {scratch}"
    assert by_label["poweroff"].argv[-1] == 'sudo -S -p "" systemctl poweroff'
    assert by_label["showvminfo"].argv == [VBOX, "showvminfo", UUID, "--machinereadable"]
    assert {record.label: record.timeout for record in guest.records} == {
        "enroll": 20, "list-unit-files": 60, "list-units": 60, "prepare": 90, "upload": 120, "capture": 600,
        **{label: 120 for label in RETRIEVALS}, "cleanup": 90, "poweroff": 30, "showvminfo": 60}
    for record in guest.records:
        if record.argv[0] in ("ssh", "scp"):
            assert record.environment["DISPLAY"] == ":0"
            assert record.environment["SSH_ASKPASS_REQUIRE"] == "force"
        else:
            assert record.environment is None
        if record.label != "poweroff":
            assert record.stdin is subprocess.DEVNULL


def test_the_capture_runs_for_the_profile_timeout(live_setup, tmp_path):
    data = profile_data()
    data["capture"]["timeoutSeconds"] = 1234
    directory = tmp_path / "profile"
    directory.mkdir()
    shutil.copy(APPLIANCE / "capture-activation.sh", directory)
    guest = live_setup.guest()
    live_setup.check(guest, clone_profile.load_profile(write_json(directory / "clone-profile.json", data)))
    assert [record.timeout for record in guest.records if record.label == "capture"] == [1234]


def test_a_recording_profile_records_runner_units(live_setup):
    guest = live_setup.guest(units=f"ssh.service enabled enabled\n{RUNNER} enabled enabled\n")
    receipt = json.loads(live_setup.check(guest).read_text(encoding="utf-8"))
    assert receipt["runnerUnits"] == {"policy": "record", "observed": [RUNNER]}
    assert guest.calls == PASSING_CALLS


def test_a_refusing_profile_stops_before_the_capture(live_setup, tmp_path):
    """TEST-105: an Actions runner unit on the clone fails the check before anything runs in the guest."""
    guest = live_setup.guest(units=f"ssh.service enabled enabled\n{RUNNER} enabled enabled\n")
    refusing = profile_copy(tmp_path / "profile", runnerUnits="refuse")
    with pytest.raises(CloneError, match=f"^REQ-103: the clone has an Actions runner unit: {re.escape(RUNNER)}$"):
        live_setup.check(guest, refusing)
    assert guest.calls == ["enroll", "list-unit-files", "list-units", "acpipowerbutton", "showvminfo"]
    assert list(live_setup.clone.evidence_dir.iterdir()) == []
    assert list(live_setup.runtime.iterdir()) == []


def test_a_refusing_profile_passes_a_clone_without_runner_units(live_setup, tmp_path):
    guest = live_setup.guest()
    receipt = json.loads(live_setup.check(guest, profile_copy(tmp_path / "profile", runnerUnits="refuse"))
                         .read_text(encoding="utf-8"))
    assert receipt["runnerUnits"] == {"policy": "refuse", "observed": []}


AFTER_FAILURE = ["acpipowerbutton", "showvminfo"]


@pytest.mark.parametrize("step, message, calls", [
    ("list-unit-files", "systemctl list-unit-files failed with exit code 1", ["enroll", "list-unit-files",
                                                                                "list-units"]),
    ("prepare", "cannot prepare clone proof directory", PASSING_CALLS[:4]),
    ("upload", "cannot upload clone capture script", PASSING_CALLS[:5]),
    ("capture", "clone runtime capture failed", PASSING_CALLS[:6]),
    (f"retrieve {ABOUT}", f"cannot retrieve clone evidence: {ABOUT}", PASSING_CALLS[:9]),
    ("cleanup", "clone guest evidence cleanup failed", PASSING_CALLS[:12]),
])
def test_a_failed_step_fails_the_check(live_setup, step, message, calls):
    guest = live_setup.guest(fail={step})
    with pytest.raises(CloneError, match=f"^{re.escape(message)}$"):
        live_setup.check(guest)
    assert guest.calls == calls + AFTER_FAILURE
    assert not (live_setup.clone.evidence_dir / clone_receipt.RECEIPT_FILENAME).exists()
    assert list(live_setup.runtime.iterdir()) == []


def changed_evidence(file: str, pointer: str, value: object) -> dict:
    files = evidence()
    set_pointer(files[file], pointer, value)
    return files


@pytest.mark.parametrize("files, message", [
    (changed_evidence(ABOUT, "/edition", "Community"), "clone VIPM activation state is invalid"),
    (changed_evidence(KNOWN_ANSWER, "/host/bootId", SOURCE_UUID), "clone captures do not share one boot ID"),
    ({**evidence(), ABOUT: "{"}, f"clone evidence {ABOUT} is unreadable"),
])
def test_failed_evidence_fails_the_check(live_setup, files, message):
    guest = live_setup.guest(files=files)
    with pytest.raises(CloneError, match=f"^{re.escape(message)}"):
        live_setup.check(guest)
    assert guest.calls == PASSING_CALLS[:11] + AFTER_FAILURE
    assert not (live_setup.clone.evidence_dir / clone_receipt.RECEIPT_FILENAME).exists()


def test_every_failed_check_is_named(live_setup):
    files = changed_evidence(ABOUT, "/edition", "Community")
    set_pointer(files[RUNTIME], "/automaticLoginEnabled", True)
    with pytest.raises(CloneError, match="^clone LabVIEW package or security state is invalid; "
                                         "clone VIPM activation state is invalid$"):
        live_setup.check(live_setup.guest(files=files))


def test_enrollment_is_retried(live_setup):
    guest = live_setup.guest(enroll_failures=2)
    live_setup.check(guest)
    assert guest.calls == ["enroll", "enroll", *PASSING_CALLS]
    assert guest.known_hosts_present == [False, False, False]
    assert live_setup.sleeps == [5, 5]


def test_enrollment_gives_up(live_setup):
    guest = live_setup.guest(enroll_failures=30)
    with pytest.raises(CloneError, match="^clone SSH identity did not become ready$"):
        live_setup.check(guest)
    assert guest.calls == ["enroll"] * 30 + AFTER_FAILURE
    assert live_setup.sleeps == [5] * 30


def test_a_clone_that_ignores_poweroff_gets_the_power_button(live_setup):
    guest = live_setup.guest(poweroff_works=False)
    live_setup.check(guest)
    assert guest.calls == PASSING_CALLS[:-1] + ["showvminfo"] * 60 + AFTER_FAILURE
    assert live_setup.sleeps == [2] * 60


def test_a_clone_that_never_powers_off_fails_and_warns(live_setup, capsys):
    guest = live_setup.guest(poweroff_works=False, acpi_works=False)
    with pytest.raises(CloneError, match="^clone did not power off cleanly$"):
        live_setup.check(guest)
    polls = ["showvminfo"] * 60
    assert guest.calls == PASSING_CALLS[:-1] + polls + ["acpipowerbutton"] + polls + ["acpipowerbutton"] + polls
    assert capsys.readouterr().err == "Vagrant clone verification: WARN: clone did not power off during cleanup\n"
    assert not (live_setup.clone.evidence_dir / clone_receipt.RECEIPT_FILENAME).exists()
    assert list(live_setup.runtime.iterdir()) == []


def test_a_failed_independent_verification_removes_the_receipt(live_setup):
    def refuse(path, **options):
        assert path.is_file()
        raise ReceiptError("bad")

    guest = live_setup.guest()
    with pytest.raises(CloneError, match="^independent clone receipt verification failed: bad$"):
        live_setup.check(guest, verify=refuse)
    names = {path.name for path in live_setup.clone.evidence_dir.iterdir()}
    assert names == set(evidence())
    assert guest.calls == PASSING_CALLS


def test_a_hash_file_that_appears_meanwhile_is_kept(live_setup):
    hash_path = live_setup.clone.evidence_dir / f"{clone_receipt.RECEIPT_FILENAME}.sha256"

    def race(path, **options):
        hash_path.write_text("someone else's\n", encoding="utf-8")
        return {}

    with pytest.raises(CloneError, match="File exists"):
        live_setup.check(live_setup.guest(), verify=race)
    assert hash_path.read_text(encoding="utf-8") == "someone else's\n"
    assert not (live_setup.clone.evidence_dir / clone_receipt.RECEIPT_FILENAME).exists()


def test_verify_options_reach_the_verifier(live_setup):
    seen = []

    def record(path, **options):
        seen.append((path, options))
        return {}

    live_setup.check(live_setup.guest(), verify=record, v1_profile_sha256="pin")
    clone = live_setup.clone
    assert seen == [(clone.evidence_dir / clone_receipt.RECEIPT_FILENAME, {
        "profile": appliance(), "box_receipt": live_setup.box_receipt, "evidence_dir": clone.evidence_dir,
        "name": NAME, "uuid": UUID, "v1_profile_sha256": "pin"})]


def test_a_password_file_removed_before_poweroff_fails_the_check(live_setup):
    guest = live_setup.guest(on={"cleanup": lambda _: live_setup.clone.password_file.unlink()})
    with pytest.raises(CloneError, match="No such file"):
        live_setup.check(guest)
    assert guest.calls == PASSING_CALLS[:12] + AFTER_FAILURE
    assert not (live_setup.clone.evidence_dir / clone_receipt.RECEIPT_FILENAME).exists()


def test_the_private_directory_falls_back_to_the_system_temporary_directory(live_setup, tmp_path, monkeypatch):
    fallback = private_dir(tmp_path / "tmp")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr("tempfile.tempdir", str(fallback))
    created = []
    guest = live_setup.guest(on={"enroll": lambda _: created.extend(
        (path.name, stat.S_IMODE(path.stat().st_mode)) for path in fallback.iterdir())})
    live_setup.check(guest)
    assert len(created) == 1 and created[0][0].startswith(live.TEMPORARY_PREFIX) and created[0][1] == 0o700
    assert list(fallback.iterdir()) == []


# The appliance adapters.

def run_adapter(monkeypatch, path: Path, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", [str(path), *argv])
    return runpy.run_path(str(path), run_name="adapter")["main"]()


PASSED_THROUGH = ("box", "manifest", "public-receipt", "public-appliance-receipt", "activation-receipt",
                  "package-report", "package-artifact", "embedded-vagrantfile")


class Host:
    """``live.run_command`` on the host: storage, the box verifier, and VBoxManage."""

    def __init__(self, findmnt=(0, "/dev/nvme0n1p2\n"), lsblk=(0, "nvme\n"), box_verifier=(0, ""), info=None):
        self.answers = {"findmnt": findmnt, "lsblk": lsblk, "verify-vagrant-box-receipt.py": box_verifier}
        self.info = instance() if info is None else info
        self.calls: list[tuple[list[str], int]] = []

    def __call__(self, argv, *, timeout):
        self.calls.append((argv, timeout))
        if argv[0] == VBOX:
            if self.info == "fail":
                return 1, ""
            return 0, "\n".join(f'{key}="{value}"' for key, value in self.info.items())
        return self.answers[Path(argv[0]).name]


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    machine_id = write_json(inputs / "id", UUID)
    password = inputs / "password"
    password.write_text("guest password\n", encoding="utf-8")
    password.chmod(0o600)
    base = write_json(inputs / "base-receipt.json", {"sshHostKey": {"fingerprint": FINGERPRINT}})
    box_receipt = {
        "schema": "projectbuddy/ubuntu-vagrant-box@1", "outcome": "PASS", "box": {"sha256": BOX_SHA256},
        "source": {"vm": {"name": "appliance", "uuid": SOURCE_UUID}, "activatedSnapshot": {"uuid": ACTIVATED_UUID},
                   "snapshot": {"uuid": PACKAGE_UUID}},
        "provenanceSha256": {"baseReceiptSha256": sha256_file(base)},
    }
    state = SimpleNamespace(
        tmp_path=tmp_path, inputs=inputs, evidence_dir=private_dir(tmp_path / "evidence"), base=base,
        box_receipt=write_json(inputs / "box-receipt.json", box_receipt), box_receipt_data=box_receipt,
        machine_id=machine_id, password=password, host=Host(), checks=[], check_result=None)
    state.options = {
        "--vm-name": NAME, "--vm-uuid": UUID, "--vagrant-machine-id-file": str(machine_id), "--ssh-port": str(PORT),
        "--guest-password-file": str(password), "--base-receipt": str(base),
        "--box-receipt": str(state.box_receipt), "--evidence-dir": str(state.evidence_dir), "--vboxmanage": VBOX,
        **{f"--{name}": str(inputs / name) for name in PASSED_THROUGH},
    }

    def run_clone_check(*arguments):
        state.checks.append(arguments)
        if isinstance(state.check_result, Exception):
            raise state.check_result
        return state.evidence_dir / clone_receipt.RECEIPT_FILENAME

    observe_instance = live.observe_instance
    monkeypatch.setattr(live, "run_command", lambda argv, *, timeout: state.host(argv, timeout=timeout))
    monkeypatch.setattr(live, "observe_instance", lambda clone: observe_instance(clone, state.host))
    monkeypatch.setattr(live, "run_clone_check", run_clone_check)

    def run(*extra: str, drop: tuple[str, ...] = (), **changes: str) -> int:
        options = {key: value for key, value in state.options.items() if key not in drop}
        options.update({f"--{key.replace('_', '-')}": value for key, value in changes.items()})
        return run_adapter(monkeypatch, CLONE_ADAPTER, [*(word for pair in options.items() for word in pair), *extra])

    state.run = run
    return state


def test_the_clone_adapter_plans(adapter, capsys):
    assert adapter.run() == 0
    assert capsys.readouterr().out.splitlines() == [
        "Vagrant clone verification: PLAN", f"clone_vm_uuid={UUID}", f"ssh_port={PORT}",
        f"source_box_sha256={BOX_SHA256}", f"clone_profile=ubuntu-labview-appliance sha256={sha256_file(PROFILE_PATH)}"]
    inputs = adapter.inputs
    assert adapter.host.calls == [
        (["findmnt", "-n", "-o", "SOURCE", "-T", str(adapter.evidence_dir)], 60),
        (["lsblk", "-n", "-o", "TRAN", "/dev/nvme0n1p2"], 60),
        ([str(APPLIANCE / "verify-vagrant-box-receipt.py"), str(adapter.box_receipt), "--box", str(inputs / "box"),
          "--manifest", str(inputs / "manifest"), "--base-receipt", str(adapter.base),
          "--public-receipt", str(inputs / "public-receipt"),
          "--public-appliance-receipt", str(inputs / "public-appliance-receipt"),
          "--activation-receipt", str(inputs / "activation-receipt"),
          "--package-report", str(inputs / "package-report"), "--package-artifact", str(inputs / "package-artifact"),
          "--embedded-vagrantfile", str(inputs / "embedded-vagrantfile"), "--vm-name", "appliance",
          "--vm-uuid", SOURCE_UUID, "--activated-snapshot-uuid", ACTIVATED_UUID,
          "--package-snapshot-uuid", PACKAGE_UUID], 900),
        ([VBOX, "showvminfo", UUID, "--machinereadable"], 60),
    ]
    assert adapter.checks == []


def test_the_clone_adapter_accepts_an_uppercase_uuid(adapter, capsys):
    assert adapter.run(vm_uuid=UUID.upper()) == 0
    assert f"clone_vm_uuid={UUID}" in capsys.readouterr().out.splitlines()


def change_box(key_path: str, value: object):
    def apply(state):
        receipt = json.loads(json.dumps(state.box_receipt_data))
        set_pointer(receipt, key_path, value)
        write_json(state.box_receipt, receipt)
    return apply


def test_the_clone_adapter_passes_canonical_source_uuids_to_the_box_verifier(adapter):
    for key_path, value in (("/source/vm/uuid", SOURCE_UUID), ("/source/activatedSnapshot/uuid", ACTIVATED_UUID),
                            ("/source/snapshot/uuid", PACKAGE_UUID)):
        set_pointer(adapter.box_receipt_data, key_path, value.upper())
    write_json(adapter.box_receipt, adapter.box_receipt_data)
    assert adapter.run() == 0
    passed = adapter.host.calls[2][0]
    assert [passed[passed.index(option) + 1] for option in (
        "--vm-uuid", "--activated-snapshot-uuid", "--package-snapshot-uuid")] == [
        SOURCE_UUID, ACTIVATED_UUID, PACKAGE_UUID]


def write_base(content: object):
    def apply(state):
        write_json(state.base, content)
        change_box("/provenanceSha256/baseReceiptSha256", sha256_file(state.base))(state)
    return apply


@pytest.mark.parametrize("setup, changes, message", [
    (None, dict(profile="missing.json"), "clone profile is unreadable"),
    (None, dict(vm_uuid="clone-1"), "clone VM UUID is not a UUID"),
    (None, dict(ssh_port="80"), "SSH port must be from 1024 through 65535"),
    (lambda s: s.password.chmod(0o644), {}, "guest password file must have mode 600 or stricter"),
    (lambda s: setattr(s, "host", Host(findmnt=(1, "/dev/nvme0n1p2\n"))), {},
     "cannot resolve clone evidence block storage"),
    (lambda s: setattr(s, "host", Host(findmnt=(0, "/dev/a\n/dev/b\n"))), {},
     "cannot resolve clone evidence block storage"),
    (lambda s: setattr(s, "host", Host(findmnt=(0, "tmpfs\n"))), {}, "cannot resolve clone evidence block storage"),
    (lambda s: setattr(s, "host", Host(lsblk=(0, "sata\n"))), {}, "clone evidence is not backed by internal NVMe"),
    (lambda s: setattr(s, "host", Host(lsblk=(1, "nvme\n"))), {}, "clone evidence is not backed by internal NVMe"),
    (lambda s: write_json(s.machine_id, SOURCE_UUID), {}, "Vagrant machine ID does not match the expected clone UUID"),
    (lambda s: s.base.unlink(), {}, "base receipt is unreadable"),
    (write_base({}), {}, "'sshHostKey'"),
    (write_base({"sshHostKey": {"fingerprint": "SHA256:" + "A" * 42}}), {}, "base receipt SSH fingerprint is invalid"),
    (write_base({"sshHostKey": {"fingerprint": "SHA256:" + "A" * 43 + "="}}), {},
     "base receipt SSH fingerprint is invalid"),
    (write_base({"sshHostKey": {"fingerprint": None}}), {}, "base receipt SSH fingerprint is invalid"),
    (change_box("/schema", "projectbuddy/ubuntu-vagrant-box@2"), {}, "box receipt is not a passing governed receipt"),
    (change_box("/outcome", "FAIL"), {}, "box receipt is not a passing governed receipt"),
    (change_box("/source/vm/uuid", UUID.upper()), {}, "clean clone UUID must differ from the source appliance UUID"),
    (change_box("/source/vm/uuid", "appliance"), {}, "badly formed hexadecimal UUID string"),
    (change_box("/provenanceSha256/baseReceiptSha256", "0" * 64), {}, "base receipt does not match box provenance"),
    (change_box("/provenanceSha256", DELETE), {}, "base receipt does not match box provenance"),
    (change_box("/source/activatedSnapshot", DELETE), {}, "'activatedSnapshot'"),
    (change_box("/source/snapshot/uuid", None), {}, "one of the hex, bytes, bytes_le, fields, or int arguments"),
    (change_box("/box/sha256", "A" * 64), {}, "box receipt does not record a box SHA-256"),
    (lambda s: write_json(s.evidence_dir / ABOUT, {}), {}, "clone evidence output already exists"),
    (lambda s: setattr(s, "host", Host(box_verifier=(1, ""))), {}, "independent box receipt verification failed"),
    (lambda s: setattr(s, "host", Host(info="fail")), {}, "cannot read clone VM identity"),
    (lambda s: setattr(s, "host", Host(info=instance(name="clone-2"))), {},
     "clone VM name does not match expected identity"),
])
def test_the_clone_adapter_refuses(adapter, capsys, setup, changes, message):
    if setup is not None:
        setup(adapter)
    assert adapter.run(**changes) == 2
    error = capsys.readouterr().err
    assert error.startswith("Vagrant clone verification: FAIL: ") and message in error
    assert adapter.checks == []


def test_the_clone_adapter_stops_before_the_box_verifier_on_a_refusal(adapter, capsys):
    write_json(adapter.machine_id, SOURCE_UUID)
    assert adapter.run() == 2
    assert [Path(argv[0]).name for argv, _ in adapter.host.calls] == ["findmnt", "lsblk"]


def test_the_clone_adapter_needs_vboxmanage(adapter, capsys, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert adapter.run(drop=("--vboxmanage",)) == 127
    assert capsys.readouterr().err == "Vagrant clone verification: FAIL: VBoxManage is missing\n"
    assert [Path(argv[0]).name for argv, _ in adapter.host.calls] == ["findmnt", "lsblk"]
    monkeypatch.setattr(shutil, "which", lambda name: VBOX if name == "VBoxManage" else None)
    assert adapter.run(drop=("--vboxmanage",)) == 0


def test_the_clone_adapter_runs_the_check(adapter, capsys):
    assert adapter.run("--run") == 0
    receipt_path = adapter.evidence_dir / clone_receipt.RECEIPT_FILENAME
    assert capsys.readouterr().out == f"Vagrant clone verification: PASS: {receipt_path}\n"
    [(clone, loaded, box, box_receipt)] = adapter.checks
    assert clone == live.Clone(name=NAME, uuid=UUID, ssh_port=PORT, fingerprint=FINGERPRINT,
                               password_file=adapter.password, evidence_dir=adapter.evidence_dir, vboxmanage=VBOX)
    assert loaded == appliance()
    assert box == {"receiptSha256": sha256_file(adapter.box_receipt), "boxSha256": BOX_SHA256}
    assert box_receipt == adapter.box_receipt


@pytest.mark.parametrize("error", [CloneError("clone runtime capture failed"), OSError("clone runtime capture failed")])
def test_a_failed_check_exits_1(adapter, capsys, error):
    adapter.check_result = error
    assert adapter.run("--run") == 1
    assert capsys.readouterr().err == "Vagrant clone verification: FAIL: clone runtime capture failed\n"


def test_the_clone_adapter_uses_a_given_profile(adapter, capsys, tmp_path):
    other = profile_copy(tmp_path / "profile", name="other-appliance")
    assert adapter.run(profile=str(other.directory / "clone-profile.json")) == 0
    assert f"clone_profile=other-appliance sha256={other.sha256}" in capsys.readouterr().out


def receipt_arguments(setup: SimpleNamespace, *extra: str) -> list[str]:
    return [str(setup.path), "--box-receipt", str(setup.box_receipt), "--evidence-dir", str(setup.evidence_dir),
            "--vm-name", NAME, "--vm-uuid", UUID, *extra]


def test_the_receipt_adapter_verifies_both_schemas(tmp_path, monkeypatch, capsys):
    setup = setup_receipt(tmp_path)
    assert run_adapter(monkeypatch, RECEIPT_ADAPTER, receipt_arguments(setup)) == 0
    assert capsys.readouterr().out == f"Vagrant clone receipt: PASS: {NAME} ({UUID})\n"
    write_json(setup.path, v1_receipt(setup))
    assert run_adapter(monkeypatch, RECEIPT_ADAPTER, receipt_arguments(setup)) == 0
    assert capsys.readouterr().out == f"Vagrant clone receipt: PASS: {NAME} ({UUID})\n"


def test_the_receipt_adapter_pins_v1_receipts_to_the_appliance_profile(tmp_path, monkeypatch, capsys):
    setup = setup_receipt(tmp_path)
    write_json(setup.path, v1_receipt(setup))
    other = profile_copy(tmp_path / "profile", name="other-appliance")
    assert run_adapter(monkeypatch, RECEIPT_ADAPTER,
                       receipt_arguments(setup, "--profile", str(other.directory / "clone-profile.json"))) == 2
    assert "verifies only against its pinned profile" in capsys.readouterr().err


@pytest.mark.parametrize("change, message", [
    (lambda s: rewrite(s, lambda r: r.update(outcome="FAIL")), "receipt does not match its inputs: outcome"),
    (lambda s: (s.evidence_dir / ABOUT).unlink(), f"clone evidence {ABOUT} is unreadable"),
    (lambda s: s.path.write_text("[]", encoding="utf-8"), "clone receipt must be a JSON object"),
])
def test_the_receipt_adapter_fails(tmp_path, monkeypatch, capsys, change, message):
    setup = setup_receipt(tmp_path)
    change(setup)
    assert run_adapter(monkeypatch, RECEIPT_ADAPTER, receipt_arguments(setup)) == 2
    assert capsys.readouterr().err.startswith(f"Vagrant clone receipt: FAIL: {message}")


def test_the_receipt_adapter_refuses_a_bad_uuid_or_profile(tmp_path, monkeypatch, capsys):
    setup = setup_receipt(tmp_path)
    arguments = receipt_arguments(setup)
    arguments[arguments.index(UUID)] = "clone-1"
    assert run_adapter(monkeypatch, RECEIPT_ADAPTER, arguments) == 2
    assert capsys.readouterr().err == "Vagrant clone receipt: FAIL: clone VM UUID is not a UUID\n"
    assert run_adapter(monkeypatch, RECEIPT_ADAPTER,
                       receipt_arguments(setup, "--profile", str(tmp_path / "missing.json"))) == 2
    assert capsys.readouterr().err.startswith("Vagrant clone receipt: FAIL: clone profile is unreadable")


@pytest.mark.parametrize("path", [CLONE_ADAPTER, RECEIPT_ADAPTER])
def test_the_adapters_are_executable(path):
    assert os.access(path, os.X_OK)
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")


def test_the_receipt_adapter_runs_as_a_program(tmp_path):
    setup = setup_receipt(tmp_path)
    completed = subprocess.run([sys.executable, str(RECEIPT_ADAPTER), *receipt_arguments(setup)],
                               capture_output=True, text=True, check=False)
    assert (completed.returncode, completed.stdout) == (0, f"Vagrant clone receipt: PASS: {NAME} ({UUID})\n")


def test_the_clone_adapter_runs_as_a_program(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(CLONE_ADAPTER), "--vm-name", NAME, "--vm-uuid", "clone-1",
         "--vagrant-machine-id-file", "/x", "--ssh-port", str(PORT), "--guest-password-file", "/x",
         *(word for name in PASSED_THROUGH for word in (f"--{name}", "/x")),
         "--base-receipt", "/x", "--box-receipt", "/x", "--evidence-dir", str(tmp_path)],
        capture_output=True, text=True, check=False)
    assert (completed.returncode, completed.stderr) == (
        2, "Vagrant clone verification: FAIL: clone VM UUID is not a UUID\n")
