"""Guest-side toolchain installer: verify every digest, then install, then probe.

Nothing is installed until every declared download matches its pinned SHA-256, so a
single mismatch leaves the guest unchanged and ``apt-get`` is never called.
"""
from __future__ import annotations

import http.client
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from typing import Callable, Sequence

from .common import ChangedError, LaneBoxError, sha256_file
from .profile import Profile, Tool

PROBE_TIMEOUT = 60
APT_TIMEOUT = 1800
DOWNLOAD_TIMEOUT = 300

Runner = Callable[..., "tuple[int, str]"]
Fetcher = Callable[[str, pathlib.Path], None]


def https_fetch(url: str, destination: pathlib.Path) -> None:
    if not url.startswith("https://"):
        raise LaneBoxError(f"refusing a non-https download: {url}")
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response, destination.open("wb") as stream:
        shutil.copyfileobj(response, stream, 1024 * 1024)


def run_command(argv: Sequence[str], timeout: int = PROBE_TIMEOUT, path_prefix: str | None = None) -> tuple[int, str]:
    """Run ``argv`` without a shell and return its exit code and standard output."""
    environment = None
    if path_prefix:
        environment = dict(os.environ, PATH=f"{path_prefix}{os.pathsep}{os.environ.get('PATH', '')}")
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, env=environment,
            stdin=subprocess.DEVNULL, check=False,
        )
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""
    return completed.returncode, completed.stdout


def evaluate_probe(tool: Tool, run: Runner, path_prefix: str | None = None) -> dict:
    code, stdout = run(list(tool.probe.argv), timeout=PROBE_TIMEOUT, path_prefix=path_prefix)
    match = re.fullmatch(tool.probe.pattern, stdout.strip()) if code == 0 else None
    observed = match.group(1) if match else None
    return {
        "name": tool.name,
        "expected": tool.probe.expected,
        "observed": observed,
        "exitCode": code,
        "ok": observed == tool.probe.expected,
    }


def download_and_verify(profile: Profile, workdir: pathlib.Path, fetch: Fetcher) -> dict[str, pathlib.Path]:
    """Download every tarball and deb, then refuse unless every digest matches."""
    artifacts: dict[str, pathlib.Path] = {}
    mismatches: list[str] = []
    for tool in profile.tools:
        if tool.kind == "bundled":
            continue
        destination = workdir / tool.name / tool.artifact_name()
        try:
            destination.parent.mkdir(mode=0o700)
            fetch(tool.url, destination)
            actual = sha256_file(destination)
        except (OSError, http.client.HTTPException) as error:
            raise LaneBoxError(f"cannot download {tool.name}; nothing was installed: {error}") from error
        if actual != tool.sha256:
            mismatches.append(f"{tool.name} expected {tool.sha256} but downloaded {actual}")
        artifacts[tool.name] = destination
    if mismatches:
        raise LaneBoxError("toolchain digest mismatch; nothing was installed: " + "; ".join(mismatches))
    return artifacts


def _install_tarball(
    tool: Tool, archive_path: pathlib.Path, prefix: pathlib.Path, bindir: pathlib.Path, changed: list[str],
) -> None:
    """Check the archive and every command name first; then move it into place and link its commands.

    The tool is added to ``changed`` as soon as its directory is in place, before any command is linked.
    """
    target = prefix / f"{tool.name}-{tool.version}"
    if target.exists() or target.is_symlink():
        raise LaneBoxError(f"install target already exists: {target}")
    prefix.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=f".{tool.name}-", dir=prefix))
    try:
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                archive.extractall(staging, filter="data")
        except tarfile.TarError as error:
            raise LaneBoxError(f"{tool.name} archive is unsafe or unreadable: {error}") from error
        entries = list(staging.iterdir())
        if len(entries) != 1 or entries[0].is_symlink() or not entries[0].is_dir():
            raise LaneBoxError(f"{tool.name} archive must contain exactly one top-level directory")
        staged_bin = entries[0] / "bin"
        commands = sorted(entry.name for entry in staged_bin.iterdir()) if staged_bin.is_dir() else []
        taken = [str(bindir / name) for name in commands if (bindir / name).exists() or (bindir / name).is_symlink()]
        if taken:
            raise LaneBoxError(f"refusing to replace existing commands: {', '.join(taken)}")
        entries[0].rename(target)
        changed.append(tool.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    bindir.mkdir(parents=True, exist_ok=True)
    for name in commands:
        (bindir / name).symlink_to(target / "bin" / name)


def install_toolchain(
    profile: Profile,
    *,
    workdir: pathlib.Path,
    prefix: pathlib.Path,
    bindir: pathlib.Path,
    fetch: Fetcher = https_fetch,
    run: Runner = run_command,
) -> list[dict]:
    """Install the profile's toolchain and return the probe results.

    Raises ``LaneBoxError`` when nothing was changed, and ``ChangedError`` once anything was installed;
    an ``OSError`` is reported the same way.
    """
    artifacts = download_and_verify(profile, workdir, fetch)
    changed: list[str] = []
    try:
        for tool in profile.tools:
            if tool.kind == "tarball":
                _install_tarball(tool, artifacts[tool.name], prefix, bindir, changed)
        debs = [tool for tool in profile.tools if tool.kind == "deb"]
        if debs:
            # apt can change the guest even when it fails. Dependencies resolve from the guest's signed apt
            # sources; the pinned files are local paths.
            changed.extend(tool.name for tool in debs)
            code, _ = run(["apt-get", "install", "-y", "--no-install-recommends",
                           *(str(artifacts[tool.name]) for tool in debs)], timeout=APT_TIMEOUT)
            if code != 0:
                raise LaneBoxError(f"apt-get install failed with exit code {code}")
        results = [evaluate_probe(tool, run, str(bindir)) for tool in profile.tools]
        failed = [result["name"] for result in results if not result["ok"]]
        if failed:
            raise LaneBoxError(f"toolchain probes failed after installation: {', '.join(failed)}")
    except (LaneBoxError, OSError) as error:
        if not changed:
            if isinstance(error, LaneBoxError):
                raise
            raise LaneBoxError(f"installation failed; nothing was installed: {error}") from error
        raise ChangedError(f"{error}; the guest was already changed by: {', '.join(changed)}") from error
    return results
