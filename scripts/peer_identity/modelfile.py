"""Deterministic Ollama Modelfile generation for the projectbuddy peer identity (#217, D9).

The identity is a pinned set of repository files plus a fixed role text. Every
source carries its expected SHA-256, so a changed input fails closed instead of
silently changing the model, and identical inputs always produce byte-identical
output.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

GENERATOR_VERSION = "1"
SCHEMA = "projectbuddy/peer-identity@1"
BASE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*")
VERSION_PATTERN = re.compile(r"[0-9]+(\.[0-9]+)*")
PATH_PATTERN = re.compile(r"[A-Za-z0-9._/-]+")
MANIFEST_KEYS = {"schema", "identityVersion", "numCtx", "temperature", "seed", "maxSystemBytes", "role", "sources"}
MAX_NUM_CTX = 131072


class IdentityError(Exception):
    """The manifest or one of its sources cannot produce a trustworthy Modelfile."""


@dataclass(frozen=True)
class Source:
    path: str
    sha256: str


@dataclass(frozen=True)
class Manifest:
    identity_version: str
    num_ctx: int
    temperature: float
    seed: int
    max_system_bytes: int
    role: tuple[str, ...]
    sources: tuple[Source, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IdentityError(message)


def parse_manifest(text: str) -> Manifest:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise IdentityError(f"manifest is not JSON: {error.msg}") from error
    _require(isinstance(data, dict), "manifest must be a JSON object")
    _require(set(data) == MANIFEST_KEYS, f"manifest keys must be exactly {sorted(MANIFEST_KEYS)}")
    _require(data["schema"] == SCHEMA, f"manifest schema must be {SCHEMA}")
    version = data["identityVersion"]
    _require(isinstance(version, str) and bool(VERSION_PATTERN.fullmatch(version)), "identityVersion must look like 0.1")
    for key, low, high in (("numCtx", 1, MAX_NUM_CTX), ("seed", 0, 2**31 - 1), ("maxSystemBytes", 1, 1024 * 1024)):
        value = data[key]
        _require(isinstance(value, int) and not isinstance(value, bool) and low <= value <= high,
                 f"{key} must be an integer from {low} to {high}")
    temperature = data["temperature"]
    _require(isinstance(temperature, (int, float)) and not isinstance(temperature, bool) and 0 <= temperature <= 2,
             "temperature must be between 0 and 2")
    role = data["role"]
    _require(isinstance(role, list) and bool(role) and all(isinstance(line, str) for line in role),
             "role must be a non-empty list of strings")
    raw_sources = data["sources"]
    _require(isinstance(raw_sources, list) and bool(raw_sources), "sources must be a non-empty list")
    sources = []
    for entry in raw_sources:
        _require(isinstance(entry, dict) and set(entry) == {"path", "sha256"}, "each source needs exactly path and sha256")
        path, digest = entry["path"], entry["sha256"]
        _require(isinstance(path, str) and bool(PATH_PATTERN.fullmatch(path))
                 and not {"", ".", ".."} & set(path.split("/")),
                 f"source path must be a plain relative path inside the repository: {path!r}")
        _require(isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)), f"bad sha256 for {path}")
        sources.append(Source(path, digest))
    _require(len({source.path for source in sources}) == len(sources), "duplicate source path")
    return Manifest(version, data["numCtx"], float(temperature), data["seed"], data["maxSystemBytes"],
                    tuple(role), tuple(sources))


def normalize(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n") + "\n"


def model_tag(identity_version: str, base: str) -> str:
    _require(bool(BASE_PATTERN.fullmatch(base)), f"base model must look like name:tag, got {base!r}")
    return f"projectbuddy:{identity_version}-{base.replace(':', '-')}"


def render(manifest: Manifest, base: str, root: Path) -> str:
    tag = model_tag(manifest.identity_version, base)
    sections = [normalize("\n".join(manifest.role))]
    digests = []
    root = root.resolve()
    for source in manifest.sources:
        file = (root / source.path).resolve()
        _require(file.is_relative_to(root), f"source escapes the repository: {source.path}")
        _require(file.is_file(), f"source missing: {source.path}")
        content = file.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        _require(actual == source.sha256, f"source changed: {source.path} is {actual}, manifest pins {source.sha256}")
        sections.append(f"## Source: {source.path}\n\n" + normalize(content.decode("utf-8-sig")))
        digests.append(f"# source {source.path} sha256 {actual}")
    system = "\n".join(sections)
    _require('"""' not in system, 'system prompt (role or sources) must not contain """')
    _require(not system.startswith('"'), "system prompt must not start with a quote")
    size = len(system.encode("utf-8"))
    _require(size <= manifest.max_system_bytes, f"system prompt is {size} bytes, over maxSystemBytes {manifest.max_system_bytes}")
    return "\n".join([
        f"# {tag}: generated by New-PeerModelfile.py (generator {GENERATOR_VERSION}); do not edit.",
        *digests,
        f"FROM {base}",
        f"PARAMETER num_ctx {manifest.num_ctx}",
        f"PARAMETER temperature {manifest.temperature!r}",
        f"PARAMETER seed {manifest.seed}",
        f'SYSTEM """{system}"""',
        "",
    ])
