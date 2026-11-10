"""Workload placement in the lane's instance (#206 PR B2): the account, probes, candidate, guest files, secrets."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lane import channel, output_disk, placement, plan, supervisor  # noqa: E402
from lane.declaration import Declaration, Phase  # noqa: E402
from lane_box.common import LaneBoxError  # noqa: E402
from vagrant_clone import guest as clone_guest  # noqa: E402

ACCOUNT = "pb-workload:x:1001:1001::/home/pb-workload:/usr/sbin/nologin\n"
CANARY = "lane-secret-canary-7f3a"
MEDIUM = "01234567-89ab-cdef-0123-456789abcdef"
DISK = {"path": "/work/pb-lane-0123456789ab/lane-output.vdi", "sizeMb": 2, "format": "VDI", "variant": "Fixed",
        "controller": "SATA", "port": 1, "device": 0}


class Instance:
    """A ``run`` behind a real ``channel.Session``: each guest command is answered by its first word.

    An answer is a result, a list of results taken in turn, or a function of the words and the standard
    input. ``files`` holds what ``install`` and the secrets command wrote, for ``stat`` and ``sha256sum``.
    """

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.calls: list[tuple[list[str], int, bytes | None]] = []
        self.files: dict[str, bytes] = {}
        self.session = channel.Session(channel.Guest(Path("/run/lane"), 2299, "SHA256:" + "A" * 43), self)

    def __call__(self, argv, timeout, stdin=subprocess.DEVNULL):
        assert argv[:len(self.session.argv)] == self.session.argv
        words = shlex.split(argv[-1])
        data = stdin.read() if hasattr(stdin, "read") else None
        self.calls.append((words, timeout, data))
        answer = self.answers[words[0]]
        if callable(answer):
            return answer(words, data)
        return answer.pop(0) if isinstance(answer, list) else answer

    def words(self) -> list[list[str]]:
        return [words for words, _, _ in self.calls]


def install(instance: Instance):
    def answer(words, data):
        instance.files[words[-1]] = data
        return 0, ""
    return answer


def stat(instance: Instance, directory: str = "0 0 41ed 2 120", file_mode: str = "81a4", links: str = "1"):
    def answer(words, data):
        lines = [directory if path == supervisor.DIRECTORY
                 else f"0 0 {file_mode} {links} {len(instance.files[path])}" for path in words[3:]]
        return 0, "".join(f"{line}\n" for line in lines)
    return answer


def sha256sum(instance: Instance, corrupt: str | None = None):
    def answer(words, data):
        return 0, "".join(f"{hashlib.sha256(instance.files[path] + (b'x' if path == corrupt else b'')).hexdigest()}"
                          f"  {path}\n" for path in words[2:])
    return answer


def declaration(**changes) -> Declaration:
    values = dict(commit="0" * 40, working_directory="src", fetch=Phase((("npm", "ci"),), 300),
                  build=Phase((("npm", "run", "package"),), 600), inputs=("package.json",), outputs=("dist/a.vsix",),
                  max_output_bytes=1024 * 1024, secrets=("VSCE_PAT", "OVSX_PAT"), toolchain=(), sha256="0" * 64)
    values.update(changes)
    return Declaration(**values)


# ---------------------------------------------------------------- commands and standard input


def test_a_command_that_fails_in_the_instance_is_refused():
    instance = Instance(true=(1, ""))
    with pytest.raises(LaneBoxError, match=r"doing it failed in the instance \(exit code 1\)"):
        placement._require(instance.session, ["true"], 5, "doing it")


def test_standard_input_delivers_every_byte_then_end_of_file():
    data = os.urandom(1 << 20)
    with placement._stdin(data) as stream:
        assert stream.read() == data


def test_standard_input_that_is_never_read_does_not_block():
    with placement._stdin(b"x" * (1 << 20)):
        pass


def test_standard_input_reaches_the_command_through_the_real_runner():
    """``run_command`` and a shell that reads the joined command, as the guest's root shell does."""
    session = channel.Session(channel.Guest(Path("/run/lane"), 2299, "SHA256:" + "A" * 43), clone_guest.run_command)
    session.argv = ["sh", "-c", 'eval "$1"', "sh"]
    data = os.urandom(3 << 20)
    with placement._stdin(data) as stream:
        output = placement._require(session, ["sha256sum", "-"], 60, "hashing", stream)
    assert output.split()[0] == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- create-account


def account_instance(**changes) -> Instance:
    answers = dict(getent=[(2, ""), (0, ACCOUNT)], useradd=(0, ""), tee=(0, "pb-workload\n"), grep=(0, ""))
    answers.update(changes)
    return Instance(**answers)


def test_create_account_makes_a_new_unprivileged_account_denied_cron_and_at():
    instance = account_instance()
    assert placement.create_account(instance.session) == 1001
    assert instance.words() == [
        ["getent", "passwd", "pb-workload"],
        ["useradd", "--create-home", "--home-dir", "/home/pb-workload", "--shell", "/usr/sbin/nologin",
         "--user-group", "pb-workload"],
        ["getent", "passwd", "pb-workload"],
        ["tee", "--append", "--", "/etc/cron.deny", "/etc/at.deny"],
        ["grep", "--fixed-strings", "--line-regexp", "--quiet", "--", "pb-workload", "/etc/cron.deny"],
        ["grep", "--fixed-strings", "--line-regexp", "--quiet", "--", "pb-workload", "/etc/at.deny"],
    ]
    assert instance.calls[3][2] == b"pb-workload\n"
    assert {timeout for _, timeout, _ in instance.calls} == {clone_guest.QUERY_TIMEOUT}


@pytest.mark.parametrize("code", [0, 1, 255])
def test_create_account_refuses_an_account_that_exists_or_a_failed_lookup(code):
    instance = account_instance(getent=[(code, ACCOUNT)])
    with pytest.raises(LaneBoxError, match="already has a pb-workload account"):
        placement.create_account(instance.session)
    assert instance.words() == [["getent", "passwd", "pb-workload"]]


@pytest.mark.parametrize("line", [
    "pb-workload:x:0:1001::/home/pb-workload:/usr/sbin/nologin\n",
    "pb-workload:x:1001:0::/home/pb-workload:/usr/sbin/nologin\n",
    "pb-workload:x:1001:1001::/home/pb-workload:/bin/bash\n",
    "pb-workload:x:1001:1001::/root:/usr/sbin/nologin\n",
    ACCOUNT + ACCOUNT,
])
def test_create_account_refuses_an_account_it_did_not_create(line):
    with pytest.raises(LaneBoxError, match="not the unprivileged, no-login account"):
        placement.create_account(account_instance(getent=[(2, ""), (0, line)]).session)


@pytest.mark.parametrize("changes, message", [
    ({"useradd": (9, "")}, "creating the pb-workload account"),
    ({"tee": (1, "")}, "denying cron and at"),
    ({"grep": [(0, ""), (1, "")]}, "checking that /etc/at.deny denies pb-workload"),
])
def test_create_account_refuses_a_step_that_fails(changes, message):
    with pytest.raises(LaneBoxError, match=message):
        placement.create_account(account_instance(**changes).session)


# ---------------------------------------------------------------- probe-<tool>


TOOL = {"name": "node", "probe": {"argv": ["node", "--version"], "pattern": r"v(\S+)", "expected": "22.11.0"}}


def test_a_probe_runs_as_the_workload_account_and_returns_its_result():
    instance = Instance(runuser=(0, "v22.11.0\n"))
    assert placement.probe(instance.session, TOOL) == {
        "name": "node", "expected": "22.11.0", "observed": "22.11.0", "exitCode": 0, "ok": True}
    assert instance.calls == [(["runuser", "-u", "pb-workload", "--", "node", "--version"], 60, None)]


@pytest.mark.parametrize("result, observed", [((0, "v20.0.0\n"), "'20.0.0'"), ((1, "v22.11.0\n"), "None"),
                                              ((0, "node 22.11.0\n"), "None"), ((124, ""), "None")])
def test_a_probe_that_does_not_print_the_declared_version_stops_the_run(result, observed):
    with pytest.raises(LaneBoxError, match=rf"probe node found {observed} \(exit code {result[0]}\)"):
        placement.probe(Instance(runuser=result).session, TOOL)


# ---------------------------------------------------------------- transfer-candidate


def git(repository: Path, *arguments: str, data: bytes | None = None) -> str:
    environment = {"PATH": os.environ["PATH"], "HOME": str(repository.parent), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a",
                   "GIT_COMMITTER_EMAIL": "a@b"}
    return subprocess.run(["git", "-C", str(repository), *arguments], input=data, capture_output=True,
                          env=environment, check=True).stdout.decode("utf-8", "surrogateescape").strip()


@pytest.fixture
def candidate(tmp_path) -> tuple[Path, str]:
    repository = tmp_path / "candidate"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    (repository / "src" / "lib").mkdir(parents=True)
    (repository / "src" / "package.json").write_text("{}\n")
    (repository / "src" / "lib" / "build.sh").write_text("#!/bin/sh\n")
    (repository / "src" / "lib" / "build.sh").chmod(0o755)
    (repository / "src" / "ignored.txt").write_text("still in the commit\n")
    (repository / "src" / "subst.txt").write_text("$Format:%H$\n")
    (repository / ".gitattributes").write_text("src/ignored.txt export-ignore\nsrc/subst.txt export-subst\n")
    os.symlink("package.json", repository / "src" / "link")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "c")
    return repository, git(repository, "rev-parse", "HEAD")


def run_dir(tmp_path: Path, name: str = "run") -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    return path


def members(archive: Path) -> dict[str, tuple]:
    with tarfile.open(archive) as opened:
        return {member.name: (member.type, member.mode, member.uid, member.gid, member.uname, member.gname,
                              member.mtime, member.linkname,
                              opened.extractfile(member).read() if member.isfile() else None)
                for member in opened.getmembers()}


def test_the_candidate_archive_is_the_commit_tree_itself(candidate, tmp_path):
    repository, commit = candidate
    path, digest = placement.write_candidate("git", repository, commit, run_dir(tmp_path))
    assert path == tmp_path / "run" / "candidate.tar" and digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.stat().st_mode & 0o777 == 0o600
    root = (tarfile.DIRTYPE, 0o755, 0, 0, "", "", 0, "", None)
    assert members(path) == {
        ".gitattributes": (tarfile.REGTYPE, 0o644, 0, 0, "", "", 0, "",
                           b"src/ignored.txt export-ignore\nsrc/subst.txt export-subst\n"),
        "src": root,
        "src/ignored.txt": (tarfile.REGTYPE, 0o644, 0, 0, "", "", 0, "", b"still in the commit\n"),
        "src/lib": root,
        "src/lib/build.sh": (tarfile.REGTYPE, 0o755, 0, 0, "", "", 0, "", b"#!/bin/sh\n"),
        "src/link": (tarfile.SYMTYPE, 0o777, 0, 0, "", "", 0, "package.json", None),
        "src/package.json": (tarfile.REGTYPE, 0o644, 0, 0, "", "", 0, "", b"{}\n"),
        "src/subst.txt": (tarfile.REGTYPE, 0o644, 0, 0, "", "", 0, "", b"$Format:%H$\n"),
    }
    # The same commit gives the same bytes, whoever builds it and whenever.
    again, _ = placement.write_candidate("git", repository, commit, run_dir(tmp_path, "again"))
    assert again.read_bytes() == path.read_bytes()


def test_the_candidate_ignores_the_callers_git_variables_and_replace_refs(candidate, tmp_path, monkeypatch):
    repository, commit = candidate
    original = git(repository, "rev-parse", f"{commit}:src/package.json")
    other = git(repository, "hash-object", "-w", "--stdin", data=b"replaced\n")
    git(repository, "replace", original, other)
    assert git(repository, "show", f"{commit}:src/package.json") == "replaced"
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    git(decoy, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(decoy / ".git" / "objects"))
    path, _ = placement.write_candidate("git", repository, commit, run_dir(tmp_path))
    assert members(path)["src/package.json"][-1] == b"{}\n"


def test_the_candidate_extracts_with_the_guest_command_to_the_commit_tree(candidate, tmp_path):
    if b"GNU tar" not in subprocess.run(["tar", "--version"], capture_output=True).stdout:
        pytest.skip("needs GNU tar, as the guest has")
    repository, commit = candidate
    path, _ = placement.write_candidate("git", repository, commit, run_dir(tmp_path))
    target = tmp_path / "extracted"
    target.mkdir()
    argv = placement.extract_argv()
    assert argv[:4] == ["runuser", "-u", "pb-workload", "--"]
    with open(path, "rb") as stream:
        subprocess.run([word.replace(placement.CANDIDATE_DIRECTORY, str(target)) for word in argv[4:]],
                       stdin=stream, check=True)
    assert (target / "src" / "lib" / "build.sh").stat().st_mode & 0o111
    assert not (target / "src" / "package.json").stat().st_mode & 0o111
    assert os.readlink(target / "src" / "link") == "package.json"
    assert (target / "src" / "ignored.txt").read_text() == "still in the commit\n"
    # --touch: the extraction time, as a checkout gives, not the archive's time 0.
    assert (target / "src" / "package.json").stat().st_mtime > 0


def test_a_submodule_is_refused(candidate, tmp_path):
    repository, commit = candidate
    git(repository, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor")
    git(repository, "commit", "-q", "-m", "submodule")
    with pytest.raises(LaneBoxError, match="has vendor with mode 160000; a lane transfers no submodule"):
        placement.write_candidate("git", repository, git(repository, "rev-parse", "HEAD"), run_dir(tmp_path))
    assert not (tmp_path / "run" / "candidate.tar").exists()


def test_a_path_that_is_not_utf8_is_refused(candidate, tmp_path):
    repository, _ = candidate
    (Path(os.fsdecode(bytes(repository) + b"/\xff"))).write_text("x")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "latin")
    with pytest.raises(LaneBoxError, match=r"the candidate path b'\\xff' is not UTF-8"):
        placement.write_candidate("git", repository, git(repository, "rev-parse", "HEAD"), run_dir(tmp_path))


def test_a_link_target_that_is_not_utf8_leaves_no_archive(candidate, tmp_path):
    repository, _ = candidate
    os.symlink(b"\xff", bytes(repository) + b"/bad")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", "bad link")
    with pytest.raises(LaneBoxError, match="the link target of bad is not UTF-8"):
        placement.write_candidate("git", repository, git(repository, "rev-parse", "HEAD"), run_dir(tmp_path))
    assert list((tmp_path / "run").iterdir()) == []


def test_the_candidate_refuses_a_bad_commit_run_directory_or_existing_archive(candidate, tmp_path):
    repository, commit = candidate
    with pytest.raises(LaneBoxError, match="is not a full commit ID"):
        placement.write_candidate("git", repository, "HEAD", run_dir(tmp_path))
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(LaneBoxError):
        placement.write_candidate("git", repository, commit, shared)
    (tmp_path / "run" / "candidate.tar").write_bytes(b"earlier")
    with pytest.raises(LaneBoxError, match="already has candidate.tar"):
        placement.write_candidate("git", repository, commit, tmp_path / "run")
    assert (tmp_path / "run" / "candidate.tar").read_bytes() == b"earlier"


def test_git_that_fails_or_cannot_run_is_refused(candidate, tmp_path):
    repository, _ = candidate
    with pytest.raises(LaneBoxError, match=r"git ls-tree failed \(exit code 128\)"):
        placement.write_candidate("git", repository, "1" * 40, run_dir(tmp_path))
    with pytest.raises(LaneBoxError, match="git ls-tree did not run"):
        placement.write_candidate(str(tmp_path / "no-git"), repository, "1" * 40, tmp_path / "run")


@pytest.mark.parametrize("output, message", [
    (b"", "did not return blob"),
    (b"aa missing\n", "did not return blob"),
    (b"aa tree 0\n\n", "did not return blob"),
    (b"aa blob x\n\n", "did not return blob"),
    (b"aa blob 3\nab", "truncated"),
    (b"aa blob 2\nabc", "truncated"),
])
def test_cat_file_output_that_is_not_the_blob_is_refused(monkeypatch, output, message):
    monkeypatch.setattr(placement, "_git", lambda *arguments: output)
    with pytest.raises(LaneBoxError, match=message):
        placement.blobs("git", Path("/r"), ["aa"])


def test_blobs_are_read_in_order_even_when_one_repeats(monkeypatch):
    monkeypatch.setattr(placement, "_git", lambda *arguments: b"aa blob 1\na\nbb blob 0\n\naa blob 1\na\n")
    assert placement.blobs("git", Path("/r"), ["aa", "bb", "aa"]) == {"aa": b"a", "bb": b""}


def test_the_reader_reads_what_is_left_when_no_size_is_given():
    reader = placement._Reader(b"abcdef")
    assert reader.read(2) == b"ab" and reader.read() == b"cdef" and reader.read(1) == b""


def test_transfer_candidate_extracts_as_the_workload_account_into_a_new_directory(tmp_path):
    archive = tmp_path / "candidate.tar"
    archive.write_bytes(b"tar bytes")
    instance = Instance(runuser=[(0, ""), (0, "")])
    placement.transfer_candidate(instance.session, archive)
    assert instance.calls == [
        (["runuser", "-u", "pb-workload", "--", "mkdir", "-m", "0700", "--", "/home/pb-workload/candidate"],
         60, None),
        (["runuser", "-u", "pb-workload", "--", "tar", "--extract", "--file=-",
          "--directory=/home/pb-workload/candidate", "--no-same-owner", "--no-same-permissions", "--touch"],
         placement.TRANSFER_TIMEOUT, b"tar bytes"),
    ]


@pytest.mark.parametrize("results, message", [([(1, "")], "creating /home/pb-workload/candidate"),
                                              ([(0, ""), (2, "")], "extracting the candidate")])
def test_transfer_candidate_refuses_a_failed_step(tmp_path, results, message):
    archive = tmp_path / "candidate.tar"
    archive.write_bytes(b"")
    with pytest.raises(LaneBoxError, match=message):
        placement.transfer_candidate(Instance(runuser=results).session, archive)


# ---------------------------------------------------------------- the spec and the output device


def vm_info(**changes) -> str:
    values = {'"SATA-0-0"': '"/vms/pb-lane/box-disk001.vmdk"', '"SATA-ImageUUID-0-0"': f'"{"f" * 8}-{MEDIUM[9:]}"',
              '"SATA-1-0"': f'"{DISK["path"]}"', '"SATA-ImageUUID-1-0"': f'"{MEDIUM}"', "VMState": '"running"'}
    values.update(changes)
    return "".join(f"{key}={value}\n" for key, value in values.items() if value is not None)


def test_the_output_device_is_the_disk_the_plan_attached():
    assert placement.output_device_of(vm_info(), DISK) == plan.output_device(MEDIUM) \
        == "/dev/disk/by-id/ata-VBOX_HARDDISK_VB01234567-efcdab89"


@pytest.mark.parametrize("changes, message", [
    ({"SnapshotName": '"s"'}, "has a snapshot"),
    ({'"SnapshotName-1"': '"s"'}, "has a snapshot"),
    ({"CurrentSnapshotUUID": f'"{MEDIUM}"'}, "has a snapshot"),
    ({'"SATA-1-0"': '"/elsewhere/lane-output.vdi"'}, "SATA-1-0 does not hold the plan's output disk"),
    ({'"SATA-1-0"': None}, "SATA-1-0 does not hold"),
    ({'"SATA-ImageUUID-1-0"': None}, "is not a medium UUID"),
    ({'"SATA-ImageUUID-1-0"': '"none"'}, "is not a medium UUID"),
])
def test_an_output_disk_slot_that_is_not_the_plans_is_refused(changes, message):
    with pytest.raises(LaneBoxError, match=message):
        placement.output_device_of(vm_info(**changes), DISK)


def test_the_guest_root_is_the_working_directory_in_the_candidate():
    assert placement.guest_root(declaration(working_directory=".")) == "/home/pb-workload/candidate"
    assert placement.guest_root(declaration(working_directory="a/b")) == "/home/pb-workload/candidate/a/b"


def test_the_guest_spec_is_one_the_supervisor_accepts():
    device = plan.output_device(MEDIUM)
    text = placement.guest_spec(declaration(), device)
    assert text.endswith(b"}\n") and text.isascii()
    value = json.loads(text)
    assert value == {
        "schema": "projectbuddy/lane-guest@1", "account": "pb-workload", "root": "/home/pb-workload/candidate/src",
        "fetch": {"steps": [["npm", "ci"]], "timeoutSeconds": 300},
        "build": {"steps": [["npm", "run", "package"]], "timeoutSeconds": 600},
        "secrets": ["VSCE_PAT", "OVSX_PAT"], "outputs": ["dist/a.vsix"], "maxOutputBytes": 1024 * 1024,
        "device": device,
    }
    assert supervisor.check_spec(value).root == "/home/pb-workload/candidate/src"


def test_a_guest_spec_the_supervisor_would_refuse_is_refused():
    with pytest.raises(LaneBoxError, match="the guest spec is not valid: device must be"):
        placement.guest_spec(declaration(), "/dev/sdb")


def test_the_guest_files_are_this_engines_supervisor_and_launcher():
    files = placement.guest_files(b"{}\n")
    assert list(files) == ["supervisor.py", "output_disk.py", "lane-guest.json"]
    assert files["supervisor.py"] == (SCRIPTS / "lane" / "supervisor.py").read_bytes()
    assert files["output_disk.py"] == Path(output_disk.__file__).read_bytes()
    assert files["lane-guest.json"] == b"{}\n"


# ---------------------------------------------------------------- place-guest-files


FILES = {"supervisor.py": b"supervisor\n", "output_disk.py": b"launcher\n", "lane-guest.json": b"{}\n"}


def guest_files_instance(**changes) -> Instance:
    instance = Instance(mkdir=(0, ""))
    instance.answers.update(install=install(instance), stat=stat(instance), sha256sum=sha256sum(instance))
    instance.answers.update({key: value(instance) if callable(value) else value for key, value in changes.items()})
    return instance


def test_place_guest_files_places_each_file_in_a_new_directory_and_checks_it():
    instance = guest_files_instance()
    digests = placement.place_guest_files(instance.session, FILES)
    assert digests == {name: hashlib.sha256(content).hexdigest() for name, content in FILES.items()}
    paths = [f"/run/projectbuddy-lane/{name}" for name in FILES]
    assert instance.words() == [
        ["mkdir", "-m", "0755", "--", "/run/projectbuddy-lane"],
        *(["install", "-m", "0644", "-o", "root", "-g", "root", "/dev/stdin", path] for path in paths),
        ["stat", r"--printf=%u %g %f %h %s\n", "--", "/run/projectbuddy-lane", *paths],
        ["sha256sum", "--", *paths],
    ]
    assert [data for words, _, data in instance.calls if words[0] == "install"] == list(FILES.values())


@pytest.mark.parametrize("changes, message", [
    ({"mkdir": (1, "")}, "creating /run/projectbuddy-lane"),
    ({"install": (1, "")}, "placing supervisor.py"),
    ({"stat": lambda instance: stat(instance, directory="0 0 41ff 2 120")}, "is not root's own directory"),
    ({"stat": lambda instance: stat(instance, directory="1000 0 41ed 2 120")}, "is not root's own directory"),
    ({"stat": lambda instance: stat(instance, file_mode="81b4")}, "is not root's own file with mode 0644"),
    ({"stat": lambda instance: stat(instance, links="2")}, "is not root's own file with mode 0644"),
    ({"stat": lambda instance: lambda words, data: (0, "0 0 41ed 2 120\n")}, "printed an unexpected form"),
    ({"stat": lambda instance: lambda words, data: (0, "0 0 41ed\n" * 4)}, "printed an unexpected form"),
    ({"stat": (1, "")}, "checking the guest files"),
    ({"sha256sum": lambda instance: sha256sum(instance, corrupt="/run/projectbuddy-lane/lane-guest.json")},
     "SHA-256 is not the SHA-256 of what the engine sent"),
])
def test_place_guest_files_refuses_files_that_are_not_what_it_sent(changes, message):
    instance = guest_files_instance(**changes)
    with pytest.raises(LaneBoxError, match=message):
        placement.place_guest_files(instance.session, FILES)


def test_a_short_file_is_refused_by_its_size():
    instance = guest_files_instance()

    def short(words, data):
        instance.files[words[-1]] = data[:-1]
        return 0, ""
    instance.answers["install"] = short
    with pytest.raises(LaneBoxError, match="the sent size"):
        placement.place_guest_files(instance.session, FILES)


# ---------------------------------------------------------------- await-fetch, place-secrets, start-build


def test_run_fetch_runs_the_supervisors_fetch_mode_for_the_fetch_timeout_plus_the_grace():
    instance = Instance(**{"/usr/bin/python3": (0, "fetch: exited 0\n")})
    placement.run_fetch(instance.session, declaration())
    assert instance.calls == [(["/usr/bin/python3", "/run/projectbuddy-lane/supervisor.py", "fetch"],
                               300 + plan.HOST_DEADLINE_GRACE, None)]


@pytest.mark.parametrize("result, shown", [((1, "fetch: exited 1\n"), "fetch: exited 1"), ((1, ""), "no result"),
                                           ((0, "fetch: exited 0\nmore\n"), "more"), ((124, ""), "no result")])
def test_a_fetch_that_does_not_exit_0_stops_the_run(result, shown):
    with pytest.raises(LaneBoxError, match=rf"the fetch phase did not succeed \(exit code {result[0]}\):.*{shown}"):
        placement.run_fetch(Instance(**{"/usr/bin/python3": result}).session, declaration())


def test_the_secrets_payload_is_each_declared_name_and_value_in_order():
    environ = {"OVSX_PAT": "o=v", "VSCE_PAT": "v\udcff", "OTHER": "x"}
    assert placement.secrets_payload(["VSCE_PAT", "OVSX_PAT"], environ) == b"VSCE_PAT=v\xff\0OVSX_PAT=o=v\0"
    assert placement.secrets_payload([], environ) == b""
    assert supervisor.parse_payload(placement.secrets_payload(["OVSX_PAT"], environ), ["OVSX_PAT"]) == {
        b"OVSX_PAT": b"o=v"}


@pytest.mark.parametrize("environ, message", [({}, "VSCE_PAT is missing or empty"),
                                              ({"VSCE_PAT": ""}, "VSCE_PAT is missing or empty"),
                                              ({"VSCE_PAT": f"{CANARY}\0"}, "VSCE_PAT holds a NUL byte")])
def test_a_missing_empty_or_nul_secret_is_named_and_its_value_is_not(environ, message):
    with pytest.raises(LaneBoxError, match=message) as raised:
        placement.secrets_payload(["VSCE_PAT"], environ)
    assert CANARY not in str(raised.value)


def secrets_instance(**changes) -> Instance:
    instance = Instance()

    def place(words, data):
        instance.files[placement.SECRETS_PATH] = data
        return 0, ""
    instance.answers.update(sh=place, stat=lambda words, data: (
        0, f"0 0 8180 1 {len(instance.files[placement.SECRETS_PATH])}\n"))
    instance.answers.update(changes)
    return instance


def test_place_secrets_sends_the_payload_on_standard_input_only():
    instance = secrets_instance()
    placement.place_secrets(instance.session, ["VSCE_PAT"], {"VSCE_PAT": CANARY})
    assert instance.words() == [
        ["sh", "-c", "umask 077 && exec install -m 0600 -o root -g root /dev/stdin /run/projectbuddy-lane/secrets"],
        ["stat", r"--printf=%u %g %f %h %s\n", "--", "/run/projectbuddy-lane/secrets"],
    ]
    assert instance.calls[0][2] == f"VSCE_PAT={CANARY}\0".encode()
    assert not any(CANARY in word for words in instance.words() for word in words)


def test_place_secrets_places_an_empty_file_when_no_secret_is_declared():
    instance = secrets_instance()
    placement.place_secrets(instance.session, [], {})
    assert instance.calls[0][2] == b""


@pytest.mark.parametrize("changes, message", [
    ({"sh": (1, "")}, "placing the secrets failed"),
    ({"stat": (0, "0 0 81a4 1 16\n")}, "is not root's own file with mode 0600"),
    ({"stat": (0, "0 0 8180 1 3\n")}, "the sent size"),
    ({"stat": (0, "1000 1000 8180 1 16\n")}, "is not root's own file with mode 0600"),
])
def test_place_secrets_refuses_a_file_that_is_not_what_it_sent(changes, message):
    with pytest.raises(LaneBoxError, match=message) as raised:
        placement.place_secrets(secrets_instance(**changes).session, ["VSCE_PAT"], {"VSCE_PAT": "v" * 7})
    assert "v" * 7 not in str(raised.value)


def test_start_build_runs_the_supervisors_start_build_mode():
    instance = Instance(**{"/usr/bin/python3": (0, "start-build: pb-lane-supervisor.service started\n")})
    placement.start_build(instance.session)
    assert instance.calls == [(["/usr/bin/python3", "/run/projectbuddy-lane/supervisor.py", "start-build"],
                               supervisor.COMMAND_TIMEOUT + clone_guest.QUERY_TIMEOUT, None)]


@pytest.mark.parametrize("result", [(1, "start-build: refused\n"), (0, ""), (255, "")])
def test_a_build_that_does_not_start_stops_the_run(result):
    with pytest.raises(LaneBoxError, match=rf"the build did not start \(exit code {result[0]}\)"):
        placement.start_build(Instance(**{"/usr/bin/python3": result}).session)


def test_guest_paths_need_no_quoting_in_the_joined_command():
    for path in (placement.CANDIDATE_DIRECTORY, placement.SECRETS_PATH, placement.SUPERVISOR_PATH,
                 *placement.DENY_FILES):
        assert shlex.quote(path) == path
