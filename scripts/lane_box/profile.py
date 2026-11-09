"""Lane profile (``projectbuddy/ubuntu-lane-profile@1``): OS, hardware, and a pinned toolchain.

The ``os`` and ``virtualBox`` blocks have the same shape as the LabVIEW appliance's
``provisioning-manifest.json``. ``vm/ubuntu-labview/build-virtualbox.sh`` still requires the
appliance's own hardware values, so it cannot yet build a lane base VM from a lane profile.
A lane profile carries no LabVIEW, VIPM, license, or runner field or component.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .common import SHA256_PATTERN, LaneBoxError, sha256_file

PROFILE_SCHEMA = "projectbuddy/ubuntu-lane-profile@1"
FIELDS = ("schema", "name", "os", "virtualBox", "toolchain")
APPLIANCE_FIELDS = ("labview", "vipm", "actionsRunner")
OS_FIELDS = ("distribution", "version", "codename", "virtualBoxOsType", "iso")
ISO_FIELDS = ("filename", "url", "sha256")
HARDWARE_FIELDS = ("diskGb", "memoryMb", "cpus", "vramMb")
TOOL_FIELDS = ("name", "version", "source", "probe")
PROBE_FIELDS = ("argv", "pattern", "expected")
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._+-]*")
VERSION_PATTERN = re.compile(r"\S+")
# Components a lane image must never carry: LabVIEW, VIPM, NI packages, and the Actions runner.
FORBIDDEN_COMPONENT = re.compile(r"labview|vipm|natinst|(?:^|[^a-z0-9])ni-|actions[-._/]runner", re.IGNORECASE)
FORBIDDEN_HOSTS = frozenset({"download.ni.com"})
ARCHIVE_SUFFIXES = {"tarball": (".tar.xz", ".tar.gz", ".tgz"), "deb": (".deb",)}


class ProfileError(LaneBoxError):
    pass


@dataclass(frozen=True)
class Probe:
    argv: tuple[str, ...]
    pattern: str
    expected: str

    def as_dict(self) -> dict:
        return {"argv": list(self.argv), "pattern": self.pattern, "expected": self.expected}


@dataclass(frozen=True)
class Tool:
    name: str
    version: str
    kind: str
    url: str | None
    sha256: str | None
    bundled_in: str | None
    probe: Probe

    def source(self) -> dict:
        if self.kind == "bundled":
            return {"kind": "bundled", "in": self.bundled_in}
        return {"kind": self.kind, "url": self.url, "sha256": self.sha256}

    def artifact_name(self) -> str:
        return pathlib.PurePosixPath(urlparse(self.url or "").path).name


@dataclass(frozen=True)
class Profile:
    name: str
    os: dict
    virtual_box: dict
    tools: tuple[Tool, ...]
    sha256: str

    def toolchain_block(self) -> list[dict]:
        return [
            {"name": tool.name, "version": tool.version, "source": tool.source(), "probe": tool.probe.as_dict()}
            for tool in self.tools
        ]


def load_profile(path: pathlib.Path) -> Profile:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileError(f"lane profile is unreadable: {error}") from error
    return parse_profile(data, sha256_file(path))


def parse_profile(data: object, digest: str) -> Profile:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ProfileError("lane profile must be a JSON object")
    for field in APPLIANCE_FIELDS:
        if field in data:
            errors.append(f"lane profile carries the LabVIEW appliance field '{field}'")
    _check_fields(data, FIELDS, "lane profile", errors, ignore=APPLIANCE_FIELDS)
    if data.get("schema") != PROFILE_SCHEMA:
        errors.append(f"schema must be {PROFILE_SCHEMA}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        errors.append("name must match [a-z0-9][a-z0-9._+-]*")
    os_block = _check_os(data.get("os"), errors)
    hardware = _check_hardware(data.get("virtualBox"), errors)
    tools = _check_toolchain(data.get("toolchain"), errors)
    if errors:
        raise ProfileError("; ".join(errors))
    return Profile(name=name, os=os_block, virtual_box=hardware, tools=tools, sha256=digest)


def _check_fields(value: dict, expected: tuple[str, ...], what: str, errors: list[str], ignore=()) -> None:
    missing = [field for field in expected if field not in value]
    unknown = sorted(set(value) - set(expected) - set(ignore))
    if missing:
        errors.append(f"{what} is missing {', '.join(missing)}")
    if unknown:
        errors.append(f"{what} has unknown fields {', '.join(unknown)}")


def _https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def _check_os(value: object, errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append("os must be an object")
        return {}
    _check_fields(value, OS_FIELDS, "os", errors)
    # base-bootstrap.sh and its receipt support Ubuntu 24.04 (noble) only.
    if (value.get("distribution"), value.get("version"), value.get("codename")) != ("ubuntu", "24.04", "noble"):
        errors.append("os must be ubuntu 24.04 (noble), the release base-bootstrap.sh supports")
    if not isinstance(value.get("virtualBoxOsType"), str) or not value.get("virtualBoxOsType"):
        errors.append("os.virtualBoxOsType must be a non-empty string")
    iso = value.get("iso")
    if not isinstance(iso, dict):
        errors.append("os.iso must be an object")
    else:
        _check_fields(iso, ISO_FIELDS, "os.iso", errors)
        if not isinstance(iso.get("filename"), str) or not iso.get("filename"):
            errors.append("os.iso.filename must be a non-empty string")
        if not _https_url(iso.get("url")):
            errors.append("os.iso.url must be an https URL")
        if not isinstance(iso.get("sha256"), str) or not SHA256_PATTERN.fullmatch(iso["sha256"]):
            errors.append("os.iso.sha256 must be 64 lowercase hex digits")
    return value


def _check_hardware(value: object, errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append("virtualBox must be an object")
        return {}
    _check_fields(value, HARDWARE_FIELDS, "virtualBox", errors)
    for field in HARDWARE_FIELDS:
        number = value.get(field)
        if field in value and (isinstance(number, bool) or not isinstance(number, int) or number <= 0):
            errors.append(f"virtualBox.{field} must be a positive integer")
    return value


def _check_toolchain(value: object, errors: list[str]) -> tuple[Tool, ...]:
    if not isinstance(value, list) or not value:
        errors.append("toolchain must be a non-empty list")
        return ()
    tools: list[Tool] = []
    for index, entry in enumerate(value):
        tool = _check_tool(entry, f"toolchain[{index}]", errors)
        if tool is not None:
            tools.append(tool)
    names = [tool.name for tool in tools]
    for duplicate in sorted({name for name in names if names.count(name) > 1}):
        errors.append(f"toolchain names '{duplicate}' more than once")
    archives = {tool.name for tool in tools if tool.kind == "tarball"}
    for tool in tools:
        if tool.kind == "bundled" and tool.bundled_in not in archives:
            errors.append(f"toolchain entry '{tool.name}' is bundled in '{tool.bundled_in}', which is not a tarball entry")
    return tuple(tools)


def _check_tool(entry: object, where: str, errors: list[str]) -> Tool | None:
    if not isinstance(entry, dict):
        errors.append(f"{where} must be an object")
        return None
    before = len(errors)
    _check_fields(entry, TOOL_FIELDS, where, errors)
    name, version = entry.get("name"), entry.get("version")
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        errors.append(f"{where}.name must match [a-z0-9][a-z0-9._+-]*")
        name = where
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        errors.append(f"{where}.version must be a non-empty string without whitespace")
    source = entry.get("source")
    kind = url = sha256 = bundled_in = None
    if not isinstance(source, dict):
        errors.append(f"{where}.source must be an object")
    else:
        kind = source.get("kind")
        if kind in ARCHIVE_SUFFIXES:
            _check_fields(source, ("kind", "url", "sha256"), f"{where}.source", errors)
            url, sha256 = source.get("url"), source.get("sha256")
            if not _https_url(url):
                errors.append(f"{where}.source.url must be an https URL")
            elif not urlparse(url).path.endswith(ARCHIVE_SUFFIXES[kind]):
                errors.append(f"{where}.source.url must end with {' or '.join(ARCHIVE_SUFFIXES[kind])}")
            elif urlparse(url).hostname in FORBIDDEN_HOSTS:
                errors.append(f"toolchain entry '{name}' downloads from {urlparse(url).hostname}; "
                              "a lane profile carries no LabVIEW, VIPM, or runner component")
            if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
                errors.append(f"{where}.source.sha256 must be 64 lowercase hex digits")
        elif kind == "bundled":
            _check_fields(source, ("kind", "in"), f"{where}.source", errors)
            bundled_in = source.get("in")
            if not isinstance(bundled_in, str):
                errors.append(f"{where}.source.in must name a tarball entry")
        else:
            errors.append(f"{where}.source.kind must be tarball, deb, or bundled")
    for text in (name, version, url):
        if isinstance(text, str) and FORBIDDEN_COMPONENT.search(text):
            errors.append(f"toolchain entry '{name}' names a LabVIEW, VIPM, or runner component; "
                          "a lane profile carries none")
            break
    probe = _check_probe(entry.get("probe"), f"{where}.probe", errors)
    if len(errors) != before or probe is None:
        return None
    return Tool(name=name, version=version, kind=kind, url=url, sha256=sha256, bundled_in=bundled_in, probe=probe)


def _check_probe(value: object, where: str, errors: list[str]) -> Probe | None:
    if not isinstance(value, dict):
        errors.append(f"{where} must be an object")
        return None
    _check_fields(value, PROBE_FIELDS, where, errors)
    argv, pattern, expected = value.get("argv"), value.get("pattern"), value.get("expected")
    valid = True
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        errors.append(f"{where}.argv must be a non-empty list of non-empty strings")
        valid = False
    try:
        groups = re.compile(pattern).groups if isinstance(pattern, str) else -1
    except re.error:
        groups = -1
    if groups != 1:
        errors.append(f"{where}.pattern must be a regular expression with exactly one capture group")
        valid = False
    if not isinstance(expected, str) or not expected:
        errors.append(f"{where}.expected must be a non-empty string")
        valid = False
    return Probe(argv=tuple(argv), pattern=pattern, expected=expected) if valid else None
