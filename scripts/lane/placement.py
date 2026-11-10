"""Workload placement in the lane's instance (VITechnologies/projectbuddy#206 PR B2).

Each function is one step of the plan. The engine runs it as root over the run's SSH channel
(``channel.Session``), and it raises ``LaneBoxError`` when the guest does not end in the state the step
requires.

``create-account``
    creates the workload account, which must not exist yet, with no login shell, and denies it cron and
    at.
``probe-<tool>``
    runs one declared tool probe as the workload account and requires the declared version.
``transfer-candidate``
    writes the candidate commit's tree to the run directory as a deterministic tar, then extracts it as
    the workload account into the account's new ``candidate`` directory.
``place-guest-files``
    creates ``/run/projectbuddy-lane`` and places the supervisor, the launcher, and the run's spec in it,
    root's own and readable by the workload account, each checked by owner, mode, and SHA-256.
``await-fetch``
    runs the supervisor's ``fetch`` mode, which runs the fetch unit to its end.
``place-secrets``
    places the declared secrets as root's own file with mode 0600. The values travel on standard input,
    never in an argv, and no message holds one.
``start-build``
    runs the supervisor's ``start-build`` mode while the link is still on.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tarfile
import threading
from typing import BinaryIO, Iterator, Mapping, Sequence

from lane_box.common import LaneBoxError, require_private_directory, sha256_file
from lane_box.packager import parse_vm_info
from lane_box.toolchain import PROBE_TIMEOUT
from vagrant_clone.guest import QUERY_TIMEOUT

from . import output_disk, supervisor
from .channel import Session
from .declaration import COMMIT_PATTERN, Declaration, Phase
from .plan import GIT_TIMEOUT, HOST_DEADLINE_GRACE, output_device

WORKLOAD = "pb-workload"
WORKLOAD_HOME = f"/home/{WORKLOAD}"
NOLOGIN = "/usr/sbin/nologin"
CANDIDATE_DIRECTORY = f"{WORKLOAD_HOME}/candidate"
DENY_FILES = ("/etc/cron.deny", "/etc/at.deny")
# getent's exit code for a key that is not in the database.
GETENT_NOT_FOUND = 2
ACCOUNT_LINE = re.compile(rf"{WORKLOAD}:x:([1-9][0-9]*):([1-9][0-9]*)::{WORKLOAD_HOME}:{NOLOGIN}\n")
CANDIDATE_ARCHIVE = "candidate.tar"
TRANSFER_TIMEOUT = 1800
# The supervisor's start-build mode waits up to its own command timeout for systemd-run.
START_BUILD_TIMEOUT = supervisor.COMMAND_TIMEOUT + QUERY_TIMEOUT
# git ls-tree modes; a gitlink (160000) is a submodule, which a lane never transfers.
TREE, FILE, EXECUTABLE, SYMLINK = "040000", "100644", "100755", "120000"
TAR_MODES = {TREE: 0o755, FILE: 0o644, EXECUTABLE: 0o755, SYMLINK: 0o777}
GUEST_DIRECTORY_MODE = "41ed"  # stat's %f for a directory with mode 0755
GUEST_FILE_MODE = "81a4"  # a regular file with mode 0644
SECRETS_MODE = "8180"  # a regular file with mode 0600
# stat --printf interprets the backslash escape itself.
STAT_FORMAT = r"%u %g %f %h %s\n"
SECRETS_PATH = f"{supervisor.DIRECTORY}/{supervisor.SECRETS_NAME}"
SECRETS_COMMAND = f"umask 077 && exec install -m 0600 -o root -g root /dev/stdin {SECRETS_PATH}"
SUPERVISOR_PATH = f"{supervisor.DIRECTORY}/{supervisor.SUPERVISOR_NAME}"


def _require(session: Session, argv: Sequence[str], timeout: int, what: str,
             stdin: int | BinaryIO = subprocess.DEVNULL) -> str:
    code, output = session.command(list(argv), timeout, stdin)
    if code != 0:
        raise LaneBoxError(f"{what} failed in the instance (exit code {code})")
    return output


@contextlib.contextmanager
def _stdin(data: bytes) -> Iterator[BinaryIO]:
    """A pipe that delivers ``data`` and then end of file, fed by a thread so no payload size blocks."""
    read, write = os.pipe()

    def feed() -> None:
        try:
            with os.fdopen(write, "wb") as stream:
                stream.write(data)
        except BrokenPipeError:
            pass

    thread = threading.Thread(target=feed, daemon=True)
    thread.start()
    try:
        with os.fdopen(read, "rb") as stream:
            yield stream
    finally:
        thread.join()


# create-account and the probes

def create_account(session: Session) -> int:
    """``create-account``: a new workload account without a login shell, denied cron and at; its user ID."""
    code, _ = session.command(["getent", "passwd", WORKLOAD], QUERY_TIMEOUT)
    if code != GETENT_NOT_FOUND:
        raise LaneBoxError(f"the instance already has a {WORKLOAD} account, or getent failed (exit code {code})")
    _require(session, ["useradd", "--create-home", "--home-dir", WORKLOAD_HOME, "--shell", NOLOGIN,
                       "--user-group", WORKLOAD], QUERY_TIMEOUT, f"creating the {WORKLOAD} account")
    match = ACCOUNT_LINE.fullmatch(_require(session, ["getent", "passwd", WORKLOAD], QUERY_TIMEOUT,
                                            f"reading the {WORKLOAD} account"))
    if not match:
        raise LaneBoxError(f"the {WORKLOAD} account is not the unprivileged, no-login account the lane created")
    with _stdin(f"{WORKLOAD}\n".encode("ascii")) as stream:
        _require(session, ["tee", "--append", "--", *DENY_FILES], QUERY_TIMEOUT, "denying cron and at", stream)
    for path in DENY_FILES:
        _require(session, ["grep", "--fixed-strings", "--line-regexp", "--quiet", "--", WORKLOAD, path],
                 QUERY_TIMEOUT, f"checking that {path} denies {WORKLOAD}")
    return int(match.group(1))


def probe(session: Session, tool: Mapping) -> dict:
    """``probe-<tool>``: the declared probe, as the workload account, must print the declared version."""
    declared = tool["probe"]
    code, output = session.command(["runuser", "-u", WORKLOAD, "--", *declared["argv"]], PROBE_TIMEOUT)
    match = re.fullmatch(declared["pattern"], output.strip()) if code == 0 else None
    observed = match.group(1) if match else None
    result = {"name": tool["name"], "expected": declared["expected"], "observed": observed, "exitCode": code,
              "ok": observed == declared["expected"]}
    if not result["ok"]:
        raise LaneBoxError(f"probe {tool['name']} found {observed!r} (exit code {code}), "
                           f"not the declared {declared['expected']!r}")
    return result


# transfer-candidate

def _git_environment() -> dict[str, str]:
    """No GIT_* variable from the caller can point git at another repository or object store."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return dict(environment, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")


def _git(git: str, repository: pathlib.Path, arguments: Sequence[str], data: bytes = b"") -> bytes:
    # Replace refs could substitute any object, so git reads the objects themselves.
    try:
        completed = subprocess.run([git, "--no-replace-objects", "-C", str(repository), *arguments], input=data,
                                   capture_output=True, env=_git_environment(), timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LaneBoxError(f"git {arguments[0]} did not run: {error}") from error
    if completed.returncode != 0:
        raise LaneBoxError(f"git {arguments[0]} failed (exit code {completed.returncode})")
    return completed.stdout


def _text(value: bytes, what: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LaneBoxError(f"{what} is not UTF-8") from error


def tree_entries(git: str, repository: pathlib.Path, commit: str) -> list[tuple[str, str, str]]:
    """The commit's tree as ``(mode, object, path)``, each directory before what it holds."""
    entries = []
    for record in _git(git, repository, ["ls-tree", "-r", "-t", "-z", "--full-tree", commit]).split(b"\0")[:-1]:
        meta, _, path = record.partition(b"\t")
        mode, _, obj = meta.decode("ascii").split(" ")
        name = _text(path, f"the candidate path {path!r}")
        if mode not in TAR_MODES:
            raise LaneBoxError(f"the candidate commit has {name} with mode {mode}; a lane transfers no "
                               "submodule and no other kind of entry")
        entries.append((mode, obj, name))
    return entries


def blobs(git: str, repository: pathlib.Path, objects: Sequence[str]) -> dict[str, bytes]:
    """Each blob's content, read with ``git cat-file --batch``."""
    output = _git(git, repository, ["cat-file", "--batch"], "".join(f"{obj}\n" for obj in objects).encode("ascii"))
    contents, offset = {}, 0
    for obj in objects:
        end = output.find(b"\n", offset)
        header = output[offset:end].decode("ascii", "replace").split(" ")
        if end < 0 or len(header) != 3 or header[:2] != [obj, "blob"] or not header[2].isdigit():
            raise LaneBoxError(f"git cat-file did not return blob {obj}")
        start, offset = end + 1, end + 1 + int(header[2])
        if output[offset:offset + 1] != b"\n":
            raise LaneBoxError(f"git cat-file returned blob {obj} truncated")
        contents[obj], offset = output[start:offset], offset + 1
    return contents


def write_candidate(git: str, repository: pathlib.Path, commit: str, run_dir: pathlib.Path) -> tuple[pathlib.Path, str]:
    """The commit's tree as a new ``candidate.tar`` in the run directory, and its SHA-256.

    It is built from the tree itself, not with ``git archive``, which applies the tree's ``export-ignore``
    and ``export-subst`` attributes with no way to turn them off. The headers are deterministic: PAX
    format, owner and group 0 with no names, modification time 0, and the mode in ``TAR_MODES``.
    """
    if not COMMIT_PATTERN.fullmatch(commit):
        raise LaneBoxError(f"{commit!r} is not a full commit ID")
    require_private_directory(run_dir)
    entries = tree_entries(git, repository, commit)
    contents = blobs(git, repository, [obj for mode, obj, _ in entries if mode != TREE])
    path = run_dir / CANDIDATE_ARCHIVE
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError as error:
        raise LaneBoxError(f"the run directory already has {CANDIDATE_ARCHIVE}") from error
    try:
        with os.fdopen(descriptor, "wb") as stream, \
                tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for mode, obj, name in entries:
                info = tarfile.TarInfo(name)
                info.mode, info.mtime, info.uid, info.gid, info.uname, info.gname = TAR_MODES[mode], 0, 0, 0, "", ""
                if mode == TREE:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                elif mode == SYMLINK:
                    info.type, info.linkname = tarfile.SYMTYPE, _text(contents[obj], f"the link target of {name}")
                    archive.addfile(info)
                else:
                    info.size = len(contents[obj])
                    archive.addfile(info, _Reader(contents[obj]))
    except BaseException:
        os.unlink(path)
        raise
    return path, sha256_file(path)


class _Reader:
    """A file object over bytes, without the copy ``io.BytesIO`` makes."""

    def __init__(self, data: bytes) -> None:
        self.view, self.offset = memoryview(data), 0

    def read(self, size: int = -1) -> bytes:
        end = len(self.view) if size < 0 else self.offset + size
        chunk = self.view[self.offset:end].tobytes()
        self.offset += len(chunk)
        return chunk


def extract_argv() -> list[str]:
    return ["runuser", "-u", WORKLOAD, "--", "tar", "--extract", "--file=-", f"--directory={CANDIDATE_DIRECTORY}",
            "--no-same-owner", "--no-same-permissions", "--touch"]


def transfer_candidate(session: Session, archive: pathlib.Path) -> None:
    """``transfer-candidate``: extract the archive, as the workload account, into its new candidate directory.

    ``--touch`` gives each file the extraction time, as a checkout does, and ``--no-same-permissions``
    applies the account's umask to the archive's modes.
    """
    _require(session, ["runuser", "-u", WORKLOAD, "--", "mkdir", "-m", "0700", "--", CANDIDATE_DIRECTORY],
             QUERY_TIMEOUT, f"creating {CANDIDATE_DIRECTORY}")
    with open(archive, "rb") as stream:
        _require(session, extract_argv(), TRANSFER_TIMEOUT, "extracting the candidate", stream)


# place-guest-files

def guest_root(declaration: Declaration) -> str:
    """The working directory in the guest: the declared one, in the candidate directory."""
    if declaration.working_directory == ".":
        return CANDIDATE_DIRECTORY
    return f"{CANDIDATE_DIRECTORY}/{declaration.working_directory}"


def output_device_of(vm_info: str, disk: Mapping) -> str:
    """The output disk's ``/dev/disk/by-id`` path in the guest, from ``verify-instance``'s output.

    The slot the plan attaches the disk at must hold the plan's disk, and the instance must have no
    snapshot, since a snapshot would put a differencing image in that slot.
    """
    info = parse_vm_info(vm_info)
    if any(key.startswith(("SnapshotName", "SnapshotUUID", "CurrentSnapshot")) for key in info):
        raise LaneBoxError("the instance has a snapshot, so its output disk slot may not hold the plan's disk")
    slot = f"{disk['controller']}-{disk['port']}-{disk['device']}"
    if info.get(slot) != disk["path"]:
        raise LaneBoxError(f"{slot} does not hold the plan's output disk {disk['path']}")
    medium = info.get(f"{disk['controller']}-ImageUUID-{disk['port']}-{disk['device']}", "")
    return output_device(medium)


def _phase(phase: Phase) -> dict:
    return {"steps": [list(step) for step in phase.steps], "timeoutSeconds": phase.timeout}


def guest_spec(declaration: Declaration, device: str) -> bytes:
    """The run's ``lane-guest@1`` spec, refused unless the supervisor itself accepts it."""
    value = {
        "schema": supervisor.SPEC_SCHEMA, "account": WORKLOAD, "root": guest_root(declaration),
        "fetch": _phase(declaration.fetch), "build": _phase(declaration.build),
        "secrets": list(declaration.secrets), "outputs": list(declaration.outputs),
        "maxOutputBytes": declaration.max_output_bytes, "device": device,
    }
    try:
        supervisor.check_spec(value)
    except supervisor.Refused as error:
        raise LaneBoxError(f"the guest spec is not valid: {error}") from error
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("ascii")


def guest_files(spec: bytes) -> dict[str, bytes]:
    """The files ``place-guest-files`` places, by name: this engine's own supervisor and launcher, and the spec."""
    return {
        supervisor.SUPERVISOR_NAME: pathlib.Path(supervisor.__file__).read_bytes(),
        supervisor.LAUNCHER_NAME: pathlib.Path(output_disk.__file__).read_bytes(),
        supervisor.SPEC_NAME: spec,
    }


def _stat(session: Session, paths: Sequence[str], what: str) -> list[list[str]]:
    output = _require(session, ["stat", f"--printf={STAT_FORMAT}", "--", *paths], QUERY_TIMEOUT, what)
    lines = [line.split(" ") for line in output.splitlines()]
    if len(lines) != len(paths) or any(len(fields) != 5 for fields in lines):
        raise LaneBoxError(f"{what} printed an unexpected form")
    return lines


def place_guest_files(session: Session, files: Mapping[str, bytes]) -> dict[str, str]:
    """``place-guest-files``: a new root-owned directory and the files in it; each file's SHA-256 by name.

    ``mkdir`` fails when the directory exists, so the files land in a directory this run created.
    """
    _require(session, ["mkdir", "-m", "0755", "--", supervisor.DIRECTORY], QUERY_TIMEOUT,
             f"creating {supervisor.DIRECTORY}")
    paths = {name: f"{supervisor.DIRECTORY}/{name}" for name in files}
    for name, content in files.items():
        with _stdin(content) as stream:
            _require(session, ["install", "-m", "0644", "-o", "root", "-g", "root", "/dev/stdin", paths[name]],
                     QUERY_TIMEOUT, f"placing {name}", stream)
    directory, *placed = _stat(session, [supervisor.DIRECTORY, *paths.values()], "checking the guest files")
    if directory[:3] != ["0", "0", GUEST_DIRECTORY_MODE]:
        raise LaneBoxError(f"{supervisor.DIRECTORY} is not root's own directory with mode 0755")
    for name, fields in zip(files, placed):
        if fields != ["0", "0", GUEST_FILE_MODE, "1", str(len(files[name]))]:
            raise LaneBoxError(f"{paths[name]} is not root's own file with mode 0644, one link, and the sent size")
    digests = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    expected = "".join(f"{digests[name]}  {paths[name]}\n" for name in files)
    if _require(session, ["sha256sum", "--", *paths.values()], QUERY_TIMEOUT, "hashing the guest files") != expected:
        raise LaneBoxError("a guest file's SHA-256 is not the SHA-256 of what the engine sent")
    return digests


# await-fetch, place-secrets, and start-build

def run_fetch(session: Session, declaration: Declaration) -> None:
    """``await-fetch``: the supervisor's ``fetch`` mode, which exits 0 only when the fetch unit exited 0."""
    timeout = declaration.fetch.timeout + HOST_DEADLINE_GRACE
    code, output = session.command([supervisor.PYTHON, SUPERVISOR_PATH, "fetch"], timeout)
    if code != 0 or output != "fetch: exited 0\n":
        shown = " ".join(output.split()) or "no result"
        raise LaneBoxError(f"the fetch phase did not succeed (exit code {code}): {shown}")


def secrets_payload(names: Sequence[str], environ: Mapping[str, str]) -> bytes:
    """``NAME=VALUE`` and a NUL for each declared name, in order; a message names a secret, never its value."""
    payload = []
    for name in names:
        value = environ.get(name)
        if not value:
            raise LaneBoxError(f"declared secret {name} is missing or empty in the dispatching context")
        encoded = value.encode("utf-8", "surrogateescape")
        if b"\0" in encoded:
            raise LaneBoxError(f"declared secret {name} holds a NUL byte")
        payload.append(name.encode("ascii") + b"=" + encoded + b"\0")
    return b"".join(payload)


def place_secrets(session: Session, names: Sequence[str], environ: Mapping[str, str]) -> None:
    """``place-secrets``: the payload as root's own file, mode 0600, created under umask 077.

    The file exists even when no secret is declared: the build unit reads it as its standard input.
    """
    payload = secrets_payload(names, environ)
    with _stdin(payload) as stream:
        _require(session, ["sh", "-c", SECRETS_COMMAND], QUERY_TIMEOUT, "placing the secrets", stream)
    [fields] = _stat(session, [SECRETS_PATH], "checking the secrets file")
    if fields != ["0", "0", SECRETS_MODE, "1", str(len(payload))]:
        raise LaneBoxError(f"{SECRETS_PATH} is not root's own file with mode 0600, one link, and the sent size")


def start_build(session: Session) -> None:
    """``start-build``: the supervisor checks what it can while the link is on, then detaches the build."""
    code, output = session.command([supervisor.PYTHON, SUPERVISOR_PATH, "start-build"], START_BUILD_TIMEOUT)
    if code != 0 or output != f"start-build: {supervisor.SUPERVISOR_UNIT} started\n":
        raise LaneBoxError(f"the build did not start (exit code {code})")
