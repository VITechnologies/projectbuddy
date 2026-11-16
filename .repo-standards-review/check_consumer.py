#!/usr/bin/env python3
"""Check this repository against its repo-standards-review consumer kit.

Managed by the repo-standards-review consumer kit (templates/consumer); update it
with `cruft update`. It needs only Python 3.9+ and Git, so it runs in any CI job,
including pull requests from forks.

Checks:
- `.repo-standards-review.json` names one RSR release: a tag, a full commit, and
  a digest-pinned workbench image. Unless `--offline` is given, it must also equal
  the release manifest published for that tag (`manifest_url` in
  `.repo-standards-review/kit.json`).
- When `.cruft.json` exists, the kit release it is linked to (`checkout`) must be
  the pinned tag. A `cruft update` that was not given the new release's values
  moves the kit but keeps the old pin, which this catches without network access.
- Every tracked executable script (a `.sh` file, a file starting with `#!`, or an
  extensionless file in a top-level hook directory) checks out byte-exact or with
  LF line endings, whatever `core.autocrlf` says.
- Every tracked file matching an `exempt_paths` pattern in
  `.repo-standards-review/kit.json` checks out byte-exact (`-text`), even if a rule
  added later overrides the kit's exemption.

Only the repository's tracked `.gitattributes` files count. Git resolves them in a
throwaway repository, so system, global, and `.git/info/attributes` files have no
effect, as in the RSR evidence scanner.

Exit 0 when both hold, 1 with findings, 2 when the check cannot run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

PIN_FILE = ".repo-standards-review.json"
CRUFT_FILE = ".cruft.json"
KIT_FILE = ".repo-standards-review/kit.json"
KIT_SCHEMA = "rsr/consumer-kit@1"
EXEMPT_ATTRIBUTE = "rsr-kit-exempt"
MAX_ATTRIBUTES_BYTES = 1024 * 1024
PIN_SCHEMA = "rsr/consumer-pin@1"
PIN_FIELDS = {
    "rsr_tag": re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+"),
    "rsr_commit": re.compile(r"[0-9a-f]{40}"),
    "workbench_image": re.compile(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}"),
}
RELEASE_MANIFEST_SCHEMA = "rsr/release-manifest@1"
MAX_MANIFEST_BYTES = 64 * 1024
MANIFEST_TIMEOUT_SECONDS = 20
HOOK_DIRS = {".husky", ".githooks", "githooks", "git-hooks"}
SKIPPED_MODES = {"120000", "160000"}


class CheckError(Exception):
    """The repository cannot be inspected."""


def git(root: Path, arguments: list[str], environment: dict[str, str], stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-c", f"core.attributesFile={environment['RSR_EMPTY_ATTRIBUTES']}",
         "-C", str(root), *arguments],
        input=stdin, capture_output=True, env=environment, check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CheckError(f"git {arguments[0]} failed: {message[-1] if message else completed.returncode}")
    return completed.stdout


def pin_findings(root: Path) -> tuple[list[str], dict | None]:
    """Format findings for the pin, and the pin itself when it is well formed."""
    try:
        pin = json.loads((root / PIN_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{PIN_FILE} is missing"], None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return [f"{PIN_FILE} is unreadable: {error}"], None
    if not isinstance(pin, dict) or set(pin) != {"schema", *PIN_FIELDS}:
        return [f"{PIN_FILE} must have exactly the fields schema, {', '.join(PIN_FIELDS)}"], None
    findings = []
    if pin["schema"] != PIN_SCHEMA:
        findings.append(f"{PIN_FILE}: schema must be {PIN_SCHEMA}")
    for field, pattern in PIN_FIELDS.items():
        if not isinstance(pin[field], str) or pattern.fullmatch(pin[field]) is None:
            findings.append(f"{PIN_FILE}: {field} is not a valid release value: {pin[field]!r}")
    return findings, (None if findings else pin)


def load_kit(root: Path) -> dict:
    try:
        kit = json.loads((root / KIT_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CheckError(f"{KIT_FILE} is missing") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckError(f"{KIT_FILE} is unreadable: {error}") from None
    patterns = kit.get("exempt_paths") if isinstance(kit, dict) else None
    if (
        not isinstance(kit, dict)
        or set(kit) != {"schema", "exempt_paths", "manifest_url"}
        or kit["schema"] != KIT_SCHEMA
        or not isinstance(patterns, list)
        or any(not isinstance(item, str) or not item or any(c.isspace() for c in item) for item in patterns)
        or not isinstance(kit["manifest_url"], str)
    ):
        raise CheckError(f"{KIT_FILE} must hold schema {KIT_SCHEMA}, a list of exempt_paths patterns, and a manifest_url")
    return kit


def linkage_findings(root: Path, pin: dict) -> list[str]:
    try:
        state = json.loads((root / CRUFT_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckError(f"{CRUFT_FILE} is unreadable: {error}") from None
    checkout = state.get("checkout") if isinstance(state, dict) else None
    if checkout == pin["rsr_tag"]:
        return []
    return [
        f"{CRUFT_FILE}: the kit is linked to {checkout!r}, but {PIN_FILE} pins {pin['rsr_tag']}; run cruft update "
        "--checkout <release tag> --variables-to-update with that release's manifest values"
    ]


def _require_secure(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    local = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme != "https" and not local:
        raise CheckError(f"the manifest URL must use https: {url}")
    return url


class _SecureRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https (or local test) URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib API
        _require_secure(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def manifest_location(template: str, tag: str) -> str:
    if "{tag}" not in template:
        raise CheckError("the manifest URL must contain {tag}")
    return _require_secure(template.replace("{tag}", urllib.parse.quote(tag, safe="")))


def manifest_findings(pin: dict, template: str) -> list[str]:
    """Compare a well-formed pin with the release manifest published for its tag."""
    tag = pin["rsr_tag"]
    url = manifest_location(template, tag)
    try:
        opener = urllib.request.build_opener(_SecureRedirects)
        with opener.open(url, timeout=MANIFEST_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_MANIFEST_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return [f"{PIN_FILE}: {tag} is not a published RSR release (no manifest at {url})"]
        raise CheckError(f"could not fetch the release manifest from {url}: HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        raise CheckError(f"could not fetch the release manifest from {url}: {error}") from None
    if len(body) > MAX_MANIFEST_BYTES:
        raise CheckError(f"the release manifest at {url} is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        manifest = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CheckError(f"the release manifest at {url} is not JSON") from None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", *PIN_FIELDS}
        or manifest["schema"] != RELEASE_MANIFEST_SCHEMA
        or manifest["rsr_tag"] != tag
    ):
        raise CheckError(f"the release manifest at {url} is not a {RELEASE_MANIFEST_SCHEMA} manifest for {tag}")
    return [
        f"{PIN_FILE}: {field} {pin[field]!r} differs from the published {tag} manifest ({manifest[field]!r}); "
        "update with cruft update --variables-to-update '<rsr_tag, rsr_commit, workbench_image from the manifest>'"
        for field in PIN_FIELDS if pin[field] != manifest[field]
    ]


def _read(root: Path, path: str, environment: dict[str, str], limit: int) -> bytes:
    candidate = root / path
    if candidate.is_file() and not candidate.is_symlink():
        with candidate.open("rb") as handle:
            return handle.read(limit)
    # A tracked file missing from the working tree is read from the index.
    return git(root, ["cat-file", "blob", f":{path}"], environment)[:limit]


def is_script(path: str, head: bytes) -> bool:
    pure = PurePosixPath(path)
    if pure.suffix.lower() == ".sh" or head == b"#!":
        return True
    return not pure.suffix and len(pure.parts) > 1 and pure.parts[0].lower() in HOOK_DIRS


def effectively_lf(attributes: dict[str, str]) -> bool:
    text, eol, crlf = attributes.get("text"), attributes.get("eol"), attributes.get("crlf")
    if text == "unset" or eol == "lf":
        return True
    if eol == "crlf":
        return False
    # The legacy crlf attribute applies when text is unspecified: -crlf means -text, crlf=input means eol=lf.
    return text == "unspecified" and crlf in ("unset", "input")


def line_ending_findings(root: Path, exemptions: list[str]) -> tuple[list[str], int]:
    with tempfile.TemporaryDirectory(prefix="rsr-consumer-check-") as scratch:
        empty = Path(scratch) / "attributes"
        empty.write_bytes(b"")
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")}
        environment.update(GIT_ATTR_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0", RSR_EMPTY_ATTRIBUTES=str(empty))
        tracked: list[str] = []
        seen: set[str] = set()
        for record in git(root, ["ls-files", "-z", "--stage"], environment).split(b"\0"):
            if not record:
                continue
            metadata, _, raw_path = record.partition(b"\t")
            path = raw_path.decode("utf-8", "surrogateescape")
            if metadata.split(b" ")[0].decode("ascii") in SKIPPED_MODES or path in seen:
                continue
            seen.add(path)
            tracked.append(path)

        # Resolve attributes in a throwaway repository holding only the tracked .gitattributes files, so
        # that the target's .git/info/attributes and configuration cannot change the answer.
        resolver = Path(scratch) / "resolver"
        git(Path(scratch), ["init", "-q", str(resolver)], environment)
        for path in tracked:
            if PurePosixPath(path).name != ".gitattributes":
                continue
            content = _read(root, path, environment, MAX_ATTRIBUTES_BYTES + 1)
            if len(content) > MAX_ATTRIBUTES_BYTES:
                raise CheckError(f"{path} is larger than {MAX_ATTRIBUTES_BYTES} bytes")
            destination = resolver.joinpath(*PurePosixPath(path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        # info/attributes outranks every .gitattributes file, so no rule can hide an exempt path.
        (resolver / ".git" / "info").mkdir(exist_ok=True)
        (resolver / ".git" / "info" / "attributes").write_bytes(
            "".join(f"{pattern} {EXEMPT_ATTRIBUTE}\n" for pattern in exemptions).encode("utf-8")
        )

        scripts = [path for path in tracked if is_script(path, _read(root, path, environment, 2))]
        queried = tracked if exemptions else scripts
        if not queried:
            return [], 0
        names = b"".join(os.fsencode(path) + b"\0" for path in queried)
        fields = git(resolver, ["check-attr", "-z", "--stdin", "text", "eol", "crlf", EXEMPT_ATTRIBUTE],
                     environment, names).split(b"\0")
    attributes: dict[str, dict[str, str]] = {}
    for index in range(0, len(fields) - 2, 3):
        path = fields[index].decode("utf-8", "surrogateescape")
        attributes.setdefault(path, {})[fields[index + 1].decode()] = fields[index + 2].decode()
    if set(attributes) != set(queried) or any(len(values) != 4 for values in attributes.values()):
        raise CheckError("git check-attr did not report every path")
    findings = [f"{path}: does not check out with LF; add its directory or path to the kit answers"
                for path in scripts if not effectively_lf(attributes[path])]
    findings += [f"{path}: is exempt but does not check out byte-exact; a later rule overrides its -text"
                 for path in queried if attributes[path][EXEMPT_ATTRIBUTE] == "set" and attributes[path]["text"] != "unset"]
    return findings, len(scripts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: the current directory)")
    parser.add_argument("--offline", action="store_true",
                        help="check the pin's format only, without comparing it with the published release manifest")
    parser.add_argument("--manifest-url", help="manifest URL template with {tag} (default: manifest_url in kit.json)")
    options = parser.parse_args(argv)
    root = Path(options.root).resolve()
    try:
        kit = load_kit(root)
        findings, pin = pin_findings(root)
        if pin is not None:
            findings += linkage_findings(root, pin)
            if not options.offline:
                findings += manifest_findings(pin, options.manifest_url or kit["manifest_url"])
        line_endings, count = line_ending_findings(root, kit["exempt_paths"])
    except (CheckError, OSError, UnicodeDecodeError) as error:
        sys.stderr.write(f"UNKNOWN: {error}\n")
        return 2
    findings += line_endings
    for finding in findings:
        print(f"FINDING: {finding}")
    if findings:
        return 1
    pin_state = "is well formed (not compared online)" if options.offline else "matches the published release"
    print(f"PASS: the RSR pin {pin_state} and {count} executable scripts check out with LF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
