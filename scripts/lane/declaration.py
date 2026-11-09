"""Lane workload declaration (``projectbuddy/lane-workload@1``): REQ-090, REQ-091, and REQ-093 checks.

A declaration is data: one argument vector, the full candidate commit, a working directory, a
timeout, inputs, outputs, secret names, and a toolchain contract. ``load_declaration`` checks it
offline and returns every refusal it finds. Its digest is the SHA-256 of its RFC 8785 canonical
form, so the same content gives the same digest however the file is formatted.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass

from lane_box.profile import NAME_PATTERN, _check_fields, _check_probe

from .common import Refusal

WORKLOAD_SCHEMA = "projectbuddy/lane-workload@1"
FIELDS = ("schema", "commit", "workingDirectory", "argv", "timeoutSeconds", "inputs", "outputs", "secrets",
          "toolchain")
CONTRACT_FIELDS = ("name", "probe")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# REQ-092: a VM disk, saved state, VM settings file, or known-hosts file is never a lane output.
FORBIDDEN_OUTPUT = re.compile(r".*\.(?:vdi|vmdk|vhd|vhdx|sav|vbox)|known_hosts", re.IGNORECASE)
# RFC 8785 serializes numbers as IEEE 754 doubles; larger integers would lose precision.
SAFE_INTEGER = 2 ** 53 - 1
DECLARATION_RULES = (
    "declaration-json", "declaration-fields", "commit", "argv", "working-directory", "timeout", "input-path",
    "output-path", "output-forbidden", "secret-name", "toolchain",
)


class NotCanonical(ValueError):
    """The value has no RFC 8785 form under this schema: a float, null, duplicate key, or similar."""


@dataclass(frozen=True)
class Declaration:
    commit: str
    working_directory: str
    argv: tuple[str, ...]
    timeout: int
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    secrets: tuple[str, ...]
    toolchain: tuple[dict, ...]
    sha256: str


def canonical(value: object) -> str:
    """The RFC 8785 (JSON Canonicalization Scheme) text of a value that holds no number but integers."""
    if value is None:
        raise NotCanonical("null is not admitted")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) > SAFE_INTEGER:
            raise NotCanonical(f"the integer {value} is outside the range RFC 8785 serializes exactly")
        return str(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise NotCanonical("a string holds an unpaired surrogate") from error
        # Python escapes exactly what RFC 8785 escapes: '"', '\', and U+0000 to U+001F, in the same forms.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        # RFC 8785 sorts member names by their UTF-16 code units, not by code points.
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(f"{canonical(key)}:{canonical(value[key])}" for key in keys) + "}"
    raise NotCanonical(f"a {type(value).__name__} is not admitted")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise NotCanonical(f"the key {key!r} appears twice")
        value[key] = item
    return value


def parse_json(text: str) -> object:
    """Parse ``text``, refusing a duplicate key. ``canonical`` refuses every float, NaN, and Infinity."""
    return json.loads(text, object_pairs_hook=_unique_pairs)


def relative_path(value: object, what: str, allow_root: bool = False) -> str | None:
    """Why ``value`` is not a normalized relative POSIX path inside its directory, or None."""
    if not isinstance(value, str) or not value:
        return f"{what} must be a non-empty string"
    if "\\" in value or "\0" in value:
        return f"{what} {value!r} must use '/' separators and hold no NUL"
    path = pathlib.PurePosixPath(value)
    if path.is_absolute():
        return f"{what} {value!r} must be relative"
    if ".." in path.parts:
        return f"{what} {value!r} must not contain '..'"
    if str(path) != value:
        return f"{what} {value!r} must be in normalized form ({str(path)!r})"
    if value == "." and not allow_root:
        return f"{what} must name a path below its directory"
    return None


def _strings(value: object, what: str, rule: str, refusals: list[Refusal]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        refusals.append(Refusal(rule, f"{what} must be a list of strings"))
        return ()
    for duplicate in sorted({item for item in value if value.count(item) > 1}):
        refusals.append(Refusal(rule, f"{what} names {duplicate!r} more than once"))
    return tuple(value)


def _check_toolchain(value: object, refusals: list[Refusal]) -> tuple[dict, ...]:
    errors: list[str] = []
    if not isinstance(value, list) or not value:
        errors.append("toolchain must be a non-empty list")
        value = []
    for index, entry in enumerate(value):
        where = f"toolchain[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        _check_fields(entry, CONTRACT_FIELDS, where, errors)
        if not isinstance(entry.get("name"), str) or not NAME_PATTERN.fullmatch(entry["name"]):
            errors.append(f"{where}.name must match {NAME_PATTERN.pattern}")
        _check_probe(entry.get("probe"), f"{where}.probe", errors)
    names = [entry.get("name") for entry in value if isinstance(entry, dict)]
    for duplicate in sorted({name for name in names if isinstance(name, str) and names.count(name) > 1}):
        errors.append(f"toolchain names {duplicate!r} more than once")
    refusals.extend(Refusal("toolchain", error) for error in errors)
    return () if errors else tuple(value)


def check_declaration(data: object, digest: str) -> tuple[Declaration | None, list[Refusal]]:
    """Every REQ-090, REQ-091, and REQ-093 rule ``data`` breaks, and the declaration if it breaks none."""
    if not isinstance(data, dict):
        return None, [Refusal("declaration-json", "the declaration must be a JSON object")]
    refusals: list[Refusal] = []
    errors: list[str] = []
    _check_fields(data, FIELDS, "declaration", errors)
    if data.get("schema") != WORKLOAD_SCHEMA:
        errors.append(f"schema must be {WORKLOAD_SCHEMA}")
    refusals.extend(Refusal("declaration-fields", error) for error in errors)
    commit = data.get("commit")
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        refusals.append(Refusal("commit", "commit must be a full 40-digit lowercase commit SHA, "
                                          "not a branch, a tag, or an abbreviated SHA"))
    argv = data.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        refusals.append(Refusal("argv", "argv must be a non-empty argument vector (a list of non-empty "
                                        "strings), not one shell string"))
    problem = relative_path(data.get("workingDirectory"), "workingDirectory", allow_root=True)
    if problem:
        refusals.append(Refusal("working-directory", problem))
    timeout = data.get("timeoutSeconds")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        refusals.append(Refusal("timeout", "timeoutSeconds must be a positive integer"))
    inputs = _strings(data.get("inputs"), "inputs", "input-path", refusals)
    for item in inputs:
        problem = relative_path(item, "input")
        if problem:
            refusals.append(Refusal("input-path", f"{problem}; inputs are paths inside the candidate checkout"))
    outputs = _strings(data.get("outputs"), "outputs", "output-path", refusals)
    for item in outputs:
        problem = relative_path(item, "output")
        if problem:
            refusals.append(Refusal("output-path", f"{problem}; outputs are paths inside the evidence directory"))
        elif FORBIDDEN_OUTPUT.fullmatch(pathlib.PurePosixPath(item).name):
            refusals.append(Refusal("output-forbidden", f"output {item!r} names a VM disk, saved state, VM "
                                                        "settings, or known-hosts file"))
    secrets = _strings(data.get("secrets"), "secrets", "secret-name", refusals)
    for name in secrets:
        if not SECRET_NAME.fullmatch(name):
            refusals.append(Refusal("secret-name", f"secret name {name!r} must match {SECRET_NAME.pattern}"))
    toolchain = _check_toolchain(data.get("toolchain"), refusals)
    if refusals:
        return None, refusals
    return Declaration(
        commit=commit, working_directory=data["workingDirectory"], argv=tuple(argv), timeout=timeout,
        inputs=inputs, outputs=outputs, secrets=secrets, toolchain=toolchain, sha256=digest,
    ), []


def load_declaration(path: pathlib.Path) -> tuple[Declaration | None, str | None, list[Refusal]]:
    """Return the declaration, its canonical SHA-256, and every refusal.

    The digest is returned whenever the file has a canonical form, so a plan can name the
    declaration it refused.
    """
    try:
        data = parse_json(path.read_text(encoding="utf-8"))
        digest = canonical_sha256(data)
    # ValueError covers a JSON syntax error, invalid UTF-8, no canonical form, and an integer longer
    # than Python converts.
    except (OSError, ValueError, RecursionError) as error:
        return None, None, [Refusal("declaration-json", f"the declaration has no canonical form: {error}")]
    declaration, refusals = check_declaration(data, digest)
    return declaration, digest, refusals
