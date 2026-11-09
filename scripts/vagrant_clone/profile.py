"""Clone profile (``projectbuddy/ubuntu-vagrant-clone-profile@1``): what a clone must show, as data.

A profile names a capture script, the JSON evidence files the script writes, where those files record
the guest's boot ID, and the checks each file must pass. The verifier copies the script into the
clone and runs it as ``<script> --output-dir <directory>``.

A check reads one value by its RFC 6901 JSON pointer and applies one test. Equality is by JSON type:
``true`` is not ``1``, and an integer is not equal to a number with a fraction or an exponent. An
unresolved pointer fails its check.
"""
from __future__ import annotations

import json
import pathlib
import re
import uuid
from dataclasses import dataclass

from lane_box.common import LaneBoxError, sha256_file
from lane_box.profile import NAME_PATTERN, _check_fields

PROFILE_SCHEMA = "projectbuddy/ubuntu-vagrant-clone-profile@1"
FIELDS = ("schema", "name", "capture", "bootId", "checks", "runnerUnits")
OPTIONAL_FIELDS = ("receiptV1Runtime",)
CAPTURE_FIELDS = ("script", "timeoutSeconds", "files")
LOCATION_FIELDS = ("file", "pointer")
BOOT_ID_FIELDS = ("file", "pointer", "alsoIn")
TESTS = ("equals", "oneOf", "matches", "items")
ITEMS_FIELDS = ("count",)
ITEMS_OPTIONAL = ("where", "each")
RUNNER_POLICIES = ("refuse", "record")
SCRIPT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EVIDENCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.json")
POINTER = re.compile(r"(?:/(?:[^~/]|~[01])*)*")
INDEX = re.compile(r"0|[1-9][0-9]*")
MAX_TIMEOUT = 86400


class ProfileError(LaneBoxError):
    pass


@dataclass(frozen=True)
class Check:
    file: str
    pointer: str
    message: str
    test: str
    expected: object


@dataclass(frozen=True)
class Profile:
    name: str
    script: str
    timeout: int
    files: tuple[str, ...]
    boot_id: tuple[tuple[str, str], ...]
    checks: tuple[Check, ...]
    runner_units: str
    receipt_v1_runtime: dict | None
    directory: pathlib.Path
    sha256: str


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"the key {key!r} appears twice")
        value[key] = item
    return value


def _refuse_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


def parse_json(text: str) -> object:
    """Parse ``text``, refusing a duplicate key, NaN, and Infinity."""
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_refuse_constant)


def same(left: object, right: object) -> bool:
    """Equality by JSON type: a boolean is not a number, and an integer is not a float."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    return left == right


_UNRESOLVED = object()


def resolve(document: object, pointer: str) -> object:
    """The value at an RFC 6901 pointer, or ``_UNRESOLVED``."""
    value = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and INDEX.fullmatch(token) and int(token) < len(value):
            value = value[int(token)]
        else:
            return _UNRESOLVED
    return value


def load_profile(path: pathlib.Path) -> Profile:
    try:
        data = parse_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ProfileError(f"clone profile is unreadable: {error}") from error
    return parse_profile(data, path.resolve().parent, sha256_file(path))


def parse_profile(data: object, directory: pathlib.Path, digest: str) -> Profile:
    if not isinstance(data, dict):
        raise ProfileError("clone profile must be a JSON object")
    errors: list[str] = []
    _check_fields(data, FIELDS, "clone profile", errors, ignore=OPTIONAL_FIELDS)
    if data.get("schema") != PROFILE_SCHEMA:
        errors.append(f"schema must be {PROFILE_SCHEMA}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        errors.append(f"name must match {NAME_PATTERN.pattern}")
    script, timeout, files = _check_capture(data.get("capture"), errors)
    boot_id = _check_boot_id(data.get("bootId"), files, errors)
    checks = _check_checks(data.get("checks"), files, errors)
    policy = data.get("runnerUnits")
    if policy not in RUNNER_POLICIES:
        errors.append("runnerUnits must be refuse or record")
    v1_runtime = data.get("receiptV1Runtime")
    if "receiptV1Runtime" in data and not isinstance(v1_runtime, dict):
        errors.append("receiptV1Runtime must be an object")
    if errors:
        raise ProfileError("; ".join(errors))
    return Profile(name=name, script=script, timeout=timeout, files=files, boot_id=boot_id, checks=checks,
                   runner_units=policy, receipt_v1_runtime=v1_runtime, directory=directory, sha256=digest)


def _check_capture(value: object, errors: list[str]) -> tuple[str, int, tuple[str, ...]]:
    if not isinstance(value, dict):
        errors.append("capture must be an object")
        return "", 0, ()
    _check_fields(value, CAPTURE_FIELDS, "capture", errors)
    script, timeout, files = value.get("script"), value.get("timeoutSeconds"), value.get("files")
    if not isinstance(script, str) or not SCRIPT_NAME.fullmatch(script):
        errors.append("capture.script must be a file name in the profile's directory")
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
        errors.append(f"capture.timeoutSeconds must be an integer from 1 through {MAX_TIMEOUT}")
    if (not isinstance(files, list) or not files
            or not all(isinstance(name, str) and EVIDENCE_NAME.fullmatch(name) for name in files)
            or len(set(files)) != len(files)):
        errors.append("capture.files must be a non-empty list of distinct .json file names")
        files = []
    return script, timeout, tuple(files)


def _check_location(value: object, files: tuple[str, ...], where: str, errors: list[str],
                    expected: tuple[str, ...] = LOCATION_FIELDS) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        errors.append(f"{where} must be an object")
        return None
    _check_fields(value, expected, where, errors)
    file, pointer = value.get("file"), value.get("pointer")
    valid = True
    if file not in files:
        errors.append(f"{where}.file must be one of capture.files")
        valid = False
    if not isinstance(pointer, str) or not POINTER.fullmatch(pointer):
        errors.append(f"{where}.pointer must be a JSON pointer")
        valid = False
    return (file, pointer) if valid else None


def _check_boot_id(value: object, files: tuple[str, ...], errors: list[str]) -> tuple[tuple[str, str], ...]:
    primary = _check_location(value, files, "bootId", errors, BOOT_ID_FIELDS)
    others = value.get("alsoIn") if isinstance(value, dict) else None
    if not isinstance(others, list) or not others:
        errors.append("bootId.alsoIn must be a non-empty list")
        return ()
    locations = [_check_location(item, files, f"bootId.alsoIn[{index}]", errors) for index, item in enumerate(others)]
    return (primary, *locations) if primary and all(locations) else ()


def _check_checks(value: object, files: tuple[str, ...], errors: list[str]) -> tuple[Check, ...]:
    if not isinstance(value, list) or not value:
        errors.append("checks must be a non-empty list")
        return ()
    checks = []
    for index, entry in enumerate(value):
        where = f"checks[{index}]"
        tests = [name for name in TESTS if isinstance(entry, dict) and name in entry]
        if len(tests) != 1:
            errors.append(f"{where} must have exactly one of {', '.join(TESTS)}")
            continue
        location = _check_location(entry, files, where, errors, (*LOCATION_FIELDS, "message", tests[0]))
        message = entry.get("message")
        if not isinstance(message, str) or not message.strip():
            errors.append(f"{where}.message must be a non-empty string")
        expected = entry[tests[0]]
        if not _check_test(tests[0], expected, where, errors) or not location or not isinstance(message, str):
            continue
        checks.append(Check(file=location[0], pointer=location[1], message=message, test=tests[0], expected=expected))
    return tuple(checks)


def _check_test(test: str, expected: object, where: str, errors: list[str]) -> bool:
    if test == "oneOf" and (not isinstance(expected, list) or not expected):
        errors.append(f"{where}.oneOf must be a non-empty list")
        return False
    if test == "matches":
        try:
            if not isinstance(expected, str):
                raise re.error("not a string")
            re.compile(expected)
        except re.error:
            errors.append(f"{where}.matches must be a regular expression")
            return False
    if test == "items":
        if not isinstance(expected, dict):
            errors.append(f"{where}.items must be an object")
            return False
        before = len(errors)
        _check_fields(expected, ITEMS_FIELDS, f"{where}.items", errors, ignore=ITEMS_OPTIONAL)
        count = expected.get("count")
        if type(count) is not int or count < 0:
            errors.append(f"{where}.items.count must be a non-negative integer")
        for field in ITEMS_OPTIONAL:
            if field in expected and not isinstance(expected[field], dict):
                errors.append(f"{where}.items.{field} must be an object")
        return len(errors) == before
    return True


def _passes(check: Check, value: object) -> bool:
    if value is _UNRESOLVED:
        return False
    if check.test == "equals":
        return same(value, check.expected)
    if check.test == "oneOf":
        return any(same(value, option) for option in check.expected)
    if check.test == "matches":
        return isinstance(value, str) and re.search(check.expected, value) is not None
    if not isinstance(value, list):
        return False
    where, each = check.expected.get("where"), check.expected.get("each")
    if where is not None and not all(isinstance(item, dict) for item in value):
        return False
    selected = [item for item in value if where is None or _has(item, where)]
    return len(selected) == check.expected["count"] and (
        each is None or all(isinstance(item, dict) and _has(item, each) for item in selected))


def _has(item: dict, fields: dict) -> bool:
    return all(key in item and same(item[key], expected) for key, expected in fields.items())


def load_evidence(profile: Profile, directory: pathlib.Path) -> dict[str, object]:
    """Each evidence file, parsed. An unreadable or invalid file is a ``ProfileError``."""
    evidence = {}
    for name in profile.files:
        try:
            evidence[name] = parse_json((directory / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise ProfileError(f"clone evidence {name} is unreadable: {error}") from error
    return evidence


def failures(profile: Profile, evidence: dict[str, object]) -> list[str]:
    """The message of each failed check, once each, in profile order."""
    messages: list[str] = []
    for check in profile.checks:
        if not _passes(check, resolve(evidence[check.file], check.pointer)) and check.message not in messages:
            messages.append(check.message)
    return messages


def boot_id(profile: Profile, evidence: dict[str, object]) -> str:
    """The guest boot ID that every named location records, in canonical form."""
    (file, pointer), *others = profile.boot_id
    value = resolve(evidence[file], pointer)
    try:
        if not isinstance(value, str):
            raise ValueError
        canonical = str(uuid.UUID(value))
    except ValueError as error:
        raise ProfileError(f"the boot ID in {file} is not a UUID") from error
    if not all(same(resolve(evidence[name], where), canonical) for name, where in others):
        raise ProfileError("clone captures do not share one boot ID")
    return canonical
