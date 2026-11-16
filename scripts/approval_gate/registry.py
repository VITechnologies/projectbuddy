"""Action classes: the closed list of privileged actions a request may ask for.

An adopting repository declares its classes in a JSON registry
(`approval-gate/classes@1`, default path `.github/approval-gate/classes.json`)
read from the gate repository's trusted ref, never from request inputs. Each
class names one built-in executor; an adopter cannot add executor code
through the registry. When no registry file exists, only the no-op
known-answer class is available.

Two tokens keep repository names out of engine code (REQ-100): `@self` in
`targets` means the repository running the gate, and `@default` in `refs`
means that repository's default branch.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import REGISTRY_SCHEMA
from .model import ACTION_CLASS_RE, CONTENT_KINDS, PRECONDITION_KINDS

EXECUTORS = ("noop",)
REGISTRY_KEYS = ("schema", "classes")
CLASS_KEYS = ("executor", "environment", "targets", "refs", "content_kinds", "precondition_kinds",
              "default_expiry_hours", "max_expiry_hours", "allow_digest_only", "description")
MAX_EXPIRY_HOURS = 720  # GitHub fails a job waiting for review after 30 days.
DEFAULT_EXPIRY_HOURS = 72
ENVIRONMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
CLASS_NAME_RE = ACTION_CLASS_RE


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ActionClass:
    name: str
    executor: str
    environment: str
    targets: "tuple[str, ...]"
    refs: "tuple[str, ...]"
    content_kinds: "tuple[str, ...]"
    precondition_kinds: "tuple[str, ...]"
    default_expiry_hours: int
    max_expiry_hours: int
    allow_digest_only: bool
    description: str

    def target_allowed(self, repository: str, ref: str, *, self_repository: str, default_branch: Optional[str]) -> bool:
        repo_ok = any(
            (pattern == "@self" and repository.lower() == self_repository.lower())
            or (pattern != "@self" and fnmatch.fnmatchcase(repository.lower(), pattern.lower()))
            for pattern in self.targets
        )
        ref_ok = any(
            (pattern == "@default" and default_branch is not None and ref == f"refs/heads/{default_branch}")
            or (pattern != "@default" and fnmatch.fnmatchcase(ref, pattern))
            for pattern in self.refs
        )
        return repo_ok and ref_ok

    def concrete_targets(self, *, self_repository: str, default_branch: Optional[str]) -> "list[tuple[str, str]]":
        """Targets that can be checked one by one; wildcard patterns are not enumerable."""
        concrete = []
        for target in self.targets:
            repository = self_repository if target == "@self" else target
            if any(c in repository for c in "*?["):
                continue
            for ref in self.refs:
                if ref == "@default":
                    if default_branch:
                        concrete.append((repository, f"refs/heads/{default_branch}"))
                elif not any(c in ref for c in "*?["):
                    concrete.append((repository, ref))
        return concrete

    def has_wildcards(self) -> bool:
        return any(any(c in p for c in "*?[") for p in (*self.targets, *self.refs))

    def to_dict(self) -> dict:
        return {
            "executor": self.executor,
            "environment": self.environment,
            "targets": list(self.targets),
            "refs": list(self.refs),
            "content_kinds": list(self.content_kinds),
            "precondition_kinds": list(self.precondition_kinds),
            "default_expiry_hours": self.default_expiry_hours,
            "max_expiry_hours": self.max_expiry_hours,
            "allow_digest_only": self.allow_digest_only,
            "description": self.description,
        }


NOOP_CLASS = ActionClass(
    name="noop",
    executor="noop",
    environment="approval-noop",
    targets=("@self",),
    refs=("@default",),
    content_kinds=CONTENT_KINDS,
    precondition_kinds=PRECONDITION_KINDS,
    default_expiry_hours=DEFAULT_EXPIRY_HOURS,
    max_expiry_hours=DEFAULT_EXPIRY_HOURS,
    allow_digest_only=True,
    description="Known-answer class: verifies the approval and the content, then writes nothing.",
)


def default_registry() -> "dict[str, ActionClass]":
    return {NOOP_CLASS.name: NOOP_CLASS}


def load_registry(path: Optional[Path]) -> "dict[str, ActionClass]":
    if path is None or not path.exists():
        return default_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RegistryError(f"{path}: not valid JSON ({exc})") from None
    except OSError as exc:
        raise RegistryError(f"{path}: cannot be read ({exc.strerror or exc})") from None
    return parse_registry(data, source=str(path))


def parse_registry(data: object, *, source: str = "registry") -> "dict[str, ActionClass]":
    if not isinstance(data, dict) or data.get("schema") != REGISTRY_SCHEMA:
        raise RegistryError(f"{source}: schema must be {REGISTRY_SCHEMA!r}")
    _no_unknown_keys(data, REGISTRY_KEYS, source)
    classes = data.get("classes")
    if not isinstance(classes, dict) or not classes:
        raise RegistryError(f"{source}: classes must be a non-empty object")
    registry = {}
    environments: dict = {}
    for name, raw in classes.items():
        action_class = _parse_class(name, raw, source)
        other = environments.get(action_class.environment.lower())
        if other:
            raise RegistryError(f"{source}: classes {other!r} and {name!r} share environment "
                                f"{action_class.environment!r}; use one environment per class")
        environments[action_class.environment.lower()] = name
        registry[name] = action_class
    return registry


def _parse_class(name: str, raw: object, source: str) -> ActionClass:
    where = f"{source}: class {name!r}"
    if not CLASS_NAME_RE.match(name):
        raise RegistryError(f"{where}: name must be lowercase letters, digits, or '-'")
    if not isinstance(raw, dict):
        raise RegistryError(f"{where}: must be an object")
    # A misspelled key would otherwise fall back to a default, which can be looser than intended.
    _no_unknown_keys(raw, CLASS_KEYS, where)
    executor = raw.get("executor")
    if executor not in EXECUTORS:
        raise RegistryError(f"{where}: executor must be one of {', '.join(EXECUTORS)}")
    environment = raw.get("environment")
    if not isinstance(environment, str) or not ENVIRONMENT_RE.match(environment):
        raise RegistryError(f"{where}: environment must be a GitHub environment name")
    targets = _string_list(raw.get("targets"), f"{where}: targets")
    refs = _string_list(raw.get("refs"), f"{where}: refs")
    for ref in refs:
        if ref != "@default" and not ref.startswith("refs/heads/"):
            raise RegistryError(f"{where}: refs must be '@default' or patterns under refs/heads/")
    content_kinds = tuple(_string_list(raw.get("content_kinds", list(CONTENT_KINDS)), f"{where}: content_kinds"))
    if set(content_kinds) - set(CONTENT_KINDS):
        raise RegistryError(f"{where}: content_kinds must be drawn from {', '.join(CONTENT_KINDS)}")
    precondition_kinds = tuple(_string_list(raw.get("precondition_kinds", list(PRECONDITION_KINDS)), f"{where}: precondition_kinds"))
    if set(precondition_kinds) - set(PRECONDITION_KINDS):
        raise RegistryError(f"{where}: precondition_kinds must be drawn from {', '.join(PRECONDITION_KINDS)}")
    max_expiry = raw.get("max_expiry_hours", DEFAULT_EXPIRY_HOURS)
    default_expiry = raw.get("default_expiry_hours", min(DEFAULT_EXPIRY_HOURS, max_expiry if isinstance(max_expiry, int) else DEFAULT_EXPIRY_HOURS))
    for label, value in (("max_expiry_hours", max_expiry), ("default_expiry_hours", default_expiry)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_EXPIRY_HOURS:
            raise RegistryError(f"{where}: {label} must be an integer from 1 to {MAX_EXPIRY_HOURS}")
    if default_expiry > max_expiry:
        raise RegistryError(f"{where}: default_expiry_hours exceeds max_expiry_hours")
    allow_digest_only = raw.get("allow_digest_only", False)
    if not isinstance(allow_digest_only, bool):
        raise RegistryError(f"{where}: allow_digest_only must be true or false")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise RegistryError(f"{where}: description must be a string")
    return ActionClass(
        name=name,
        executor=executor,
        environment=environment,
        targets=tuple(targets),
        refs=tuple(refs),
        content_kinds=content_kinds,
        precondition_kinds=precondition_kinds,
        default_expiry_hours=default_expiry,
        max_expiry_hours=max_expiry,
        allow_digest_only=allow_digest_only,
        description=description,
    )


def _no_unknown_keys(raw: dict, allowed: "tuple[str, ...]", where: str) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise RegistryError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}; allowed: {', '.join(allowed)}")


def _string_list(value: object, where: str) -> "list[str]":
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise RegistryError(f"{where} must be a non-empty list of strings")
    return value
