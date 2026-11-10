"""The lane's SSH channel (#206 PR B1): the run's client key, host-key enrollment, root commands, runner units."""
from __future__ import annotations

import base64
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lane import channel  # noqa: E402
from lane_box.common import LaneBoxError  # noqa: E402
from vagrant_clone import guest as clone_guest  # noqa: E402
from vagrant_clone.core import RUNNER_UNIT_QUERIES  # noqa: E402

NAME = "pb-lane-0123456789ab"
PORT = 2299
UNITS = "ssh.service enabled enabled\nsystemd-journald.service static -\n"


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def blob(seed: bytes = b"k") -> bytes:
    return channel.BLOB_PREFIX + (seed * 32)[:32]


def encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def lane_access():
    spec = importlib.util.spec_from_file_location(
        "lane_access", SCRIPTS / "lane_box" / "guest" / "lane_access.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guest(run_dir: Path, seed: bytes = b"k") -> channel.Guest:
    return channel.Guest(run_dir, PORT, channel.fingerprint(blob(seed)))


class Recorder:
    """A ``run`` that records each call and answers from a list of results or a function."""

    def __init__(self, *results, answer=None) -> None:
        self.results = list(results)
        self.answer = answer
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **options):
        self.calls.append((list(argv), options))
        if self.answer is not None:
            return self.answer(list(argv), options)
        return self.results.pop(0)


# ---------------------------------------------------------------- the client key


def test_the_access_property_is_the_one_the_guest_reads():
    assert channel.ACCESS_PROPERTY == lane_access().PROPERTY
    assert channel.BLOB_PREFIX == lane_access().BLOB_PREFIX


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH's ssh-keygen")
def test_a_generated_client_key_is_one_the_guest_accepts(tmp_path):
    run_dir = private_dir(tmp_path / NAME)
    key = channel.generate_client_key(run_dir, NAME)
    assert (run_dir / "client-key").stat().st_mode & 0o777 == 0o600
    public = (run_dir / "client-key.pub").read_text(encoding="ascii")
    listed = subprocess.run(["ssh-keygen", "-lf", str(run_dir / "client-key.pub"), "-E", "sha256"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert key == {"algorithm": "ssh-ed25519", "fingerprint": listed[1]}
    # The value the Vagrantfile sets is the file without its newline; the guest's parser takes it.
    access = lane_access()
    line, fingerprint = access.authorized_line(f"Value: {public.rstrip()}\nTimestamp: 1 ns\nFlags: RDONLYGUEST\n")
    assert fingerprint == key["fingerprint"] and line.endswith(f" {public.split()[0]} {public.split()[1]}\n")
    with pytest.raises(LaneBoxError, match="already has a client key"):
        channel.generate_client_key(run_dir, NAME)


def written_key(run_dir: Path, public: str | bytes, mode: int = 0o600):
    def answer(argv, options):
        assert argv == ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", NAME, "-f", str(run_dir / "client-key")]
        assert options == {"timeout": channel.KEYGEN_TIMEOUT}
        private = run_dir / "client-key"
        private.write_text("private\n", encoding="ascii")
        private.chmod(mode)
        data = public.encode("ascii") if isinstance(public, str) else public
        (run_dir / "client-key.pub").write_bytes(data)
        return 0, ""
    return Recorder(answer=answer)


def test_generate_client_key_checks_what_ssh_keygen_wrote(tmp_path):
    run_dir = private_dir(tmp_path / "run")
    key = channel.generate_client_key(run_dir, NAME, written_key(run_dir, f"ssh-ed25519 {encoded(blob())} {NAME}\n"))
    assert key == {"algorithm": "ssh-ed25519", "fingerprint": channel.fingerprint(blob())}


@pytest.mark.parametrize("public, mode, message", [
    (f"ssh-ed25519 {encoded(blob())} {NAME}\n", 0o640, "group or others access"),
    (f"ssh-ed25519 {encoded(blob())} pb-lane-ffffffffffff\n", 0o600, "commented with the instance's name"),
    (f"ssh-ed25519 {encoded(blob())} {NAME}", 0o600, "commented with the instance's name"),
    (f"ssh-ed25519 {encoded(blob())} {NAME}\nssh-ed25519 {encoded(blob())} {NAME}\n", 0o600, "one ed25519"),
    (f"ssh-rsa {encoded(blob())} {NAME}\n", 0o600, "one ed25519"),
    (f"ssh-ed25519 {encoded(b'x' * 51)} {NAME}\n", 0o600, "not an ed25519 public key"),
    (b"ssh-ed25519 \xff\n", 0o600, "not ASCII"),
])
def test_generate_client_key_refuses_a_key_it_did_not_expect(tmp_path, public, mode, message):
    run_dir = private_dir(tmp_path / "run")
    with pytest.raises(LaneBoxError, match=message):
        channel.generate_client_key(run_dir, NAME, written_key(run_dir, public, mode))


def test_generate_client_key_refuses_before_it_runs_ssh_keygen(tmp_path):
    never = Recorder()
    with pytest.raises(LaneBoxError, match="not a lane instance name"):
        channel.generate_client_key(private_dir(tmp_path / "a"), "pb-lane-x", never)
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    with pytest.raises(LaneBoxError, match="mode 700"):
        channel.generate_client_key(open_dir, NAME, never)
    for existing in ("client-key", "client-key.pub"):
        run_dir = private_dir(tmp_path / f"has-{existing}")
        (run_dir / existing).symlink_to(tmp_path / "nowhere")
        with pytest.raises(LaneBoxError, match="already has a client key"):
            channel.generate_client_key(run_dir, NAME, never)
    expanding = private_dir(tmp_path / "a%h")
    with pytest.raises(LaneBoxError, match="ssh would expand or split"):
        channel.generate_client_key(expanding, NAME, never)
    assert never.calls == []


def test_generate_client_key_refuses_a_failed_or_unexpected_ssh_keygen(tmp_path):
    run_dir = private_dir(tmp_path / "failed")
    with pytest.raises(LaneBoxError, match="exit code 1"):
        channel.generate_client_key(run_dir, NAME, Recorder((1, "")))
    run_dir = private_dir(tmp_path / "missing")
    with pytest.raises(LaneBoxError, match="cannot open client-key"):
        channel.generate_client_key(run_dir, NAME, Recorder((0, "")))

    def linked(argv, options):
        (run_dir / "client-key").write_text("private\n", encoding="ascii")
        (run_dir / "client-key").chmod(0o600)
        os.link(run_dir / "client-key", run_dir / "elsewhere")
        return 0, ""
    run_dir = private_dir(tmp_path / "linked")
    with pytest.raises(LaneBoxError, match="one link"):
        channel.generate_client_key(run_dir, NAME, Recorder(answer=linked))


@pytest.mark.parametrize("data", ["", encoded(blob())[:-4], encoded(blob()) + "AAAA", encoded(blob())[:-1] + "="])
def test_key_blob_takes_only_the_68_characters_of_an_ed25519_key(data):
    with pytest.raises(LaneBoxError, match="not an ed25519 public key"):
        channel.key_blob(data)
    assert channel.key_blob(encoded(blob())) == blob()


# ---------------------------------------------------------------- the plan's instance


def ready_plan(**changes) -> dict:
    value = {
        "outcome": "ready",
        "instance": {"runDirectory": "/work/" + NAME},
        "network": {"sshForward": {"hostPort": PORT}},
        "box": {"sshHostKey": {"algorithm": "ssh-ed25519", "fingerprint": channel.fingerprint(blob())}},
    }
    value.update(changes)
    return value


def test_a_ready_plan_names_the_guest():
    assert channel.Guest.from_plan(ready_plan()) == channel.Guest(Path("/work/" + NAME), PORT,
                                                                  channel.fingerprint(blob()))


@pytest.mark.parametrize("plan, message", [
    (ready_plan(outcome="refused"), "only a ready plan"),
    (ready_plan(box={"sshHostKey": None}), "does not name"),
    (ready_plan(network={}), "does not name"),
    (ready_plan(instance={"runDirectory": 7}), "run directory or SSH port"),
    (ready_plan(network={"sshForward": {"hostPort": True}}), "run directory or SSH port"),
    (ready_plan(network={"sshForward": {"hostPort": 22}}), "run directory or SSH port"),
    (ready_plan(box={"sshHostKey": {"algorithm": "ssh-rsa", "fingerprint": channel.fingerprint(blob())}}),
     "not an ed25519"),
    (ready_plan(box={"sshHostKey": {"algorithm": "ssh-ed25519", "fingerprint": "MD5:00"}}), "not an ed25519"),
    (ready_plan(box={"sshHostKey": {"algorithm": "ssh-ed25519", "fingerprint": None}}), "not an ed25519"),
])
def test_a_plan_that_does_not_name_a_guest_is_refused(plan, message):
    with pytest.raises(LaneBoxError, match=message):
        channel.Guest.from_plan(plan)


# ---------------------------------------------------------------- the ssh argv


def test_a_session_runs_one_quoted_command_as_root_with_only_the_runs_key(tmp_path):
    run = Recorder((0, "out\n"))
    session = channel.Session(guest(tmp_path), run)
    assert session.command(["printf", "%s", "a b", "-x"], 30, stdin="input") == (0, "out\n")
    [(argv, options)] = run.calls
    assert argv[:5] == ["ssh", "-F", "none", "-p", str(PORT)]
    assert argv[-3:] == ["--", "root@127.0.0.1", "printf %s 'a b' -x"]
    options = argv[5:-3]
    assert options[::2] == ["-o"] * (len(options) // 2)
    assert options[1::2] == [f"IdentityFile={tmp_path}/client-key", f"UserKnownHostsFile={tmp_path}/known_hosts",
                             *channel.SSH_OPTIONS]
    assert "-i" not in argv and run.calls[0][1] == {"timeout": 30, "stdin": "input"}


@pytest.mark.parametrize("argv", [[], [""], ["id", 7]])
def test_a_session_refuses_a_command_that_is_not_an_argument_vector(tmp_path, argv):
    run = Recorder()
    with pytest.raises(LaneBoxError, match="non-empty list"):
        channel.Session(guest(tmp_path), run).command(argv, 30)
    assert run.calls == []


@pytest.mark.parametrize("name", ["a%h", "a b", "~a", "a'b", 'a"b', "a,b", "a\\b"])
def test_a_session_refuses_a_run_directory_that_ssh_would_expand(tmp_path, name):
    with pytest.raises(LaneBoxError, match="ssh would expand or split"):
        channel.Session(guest(tmp_path / name), Recorder())


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH's ssh")
def test_openssh_reads_the_session_argv_as_written(tmp_path):
    """``ssh -G`` parses the argv without connecting; the run's key must be its only identity."""
    session = channel.Session(guest(tmp_path / "missing"), Recorder())
    shown = subprocess.run(["ssh", "-G", *session.argv[1:]], capture_output=True, text=True, check=True,
                           stdin=subprocess.DEVNULL, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    values: dict[str, list[str]] = {}
    for line in shown.stdout.splitlines():
        key, _, value = line.partition(" ")
        values.setdefault(key, []).append(value)
    assert values["identityfile"] == [f"{tmp_path}/missing/client-key"]
    assert values["userknownhostsfile"] == [f"{tmp_path}/missing/known_hosts"]
    assert values["globalknownhostsfile"] == ["/dev/null"]
    assert values["user"] == ["root"] and values["hostname"] == ["127.0.0.1"] and values["port"] == [str(PORT)]
    for key, value in (("batchmode", "yes"), ("identitiesonly", "yes"), ("identityagent", "none"),
                       ("stricthostkeychecking", "true"), ("hostkeyalgorithms", "ssh-ed25519"),
                       ("passwordauthentication", "no"), ("kbdinteractiveauthentication", "no"),
                       ("forwardagent", "no"), ("clearallforwardings", "yes"), ("requesttty", "false"),
                       ("controlmaster", "false"), ("updatehostkeys", "false"), ("checkhostip", "no"),
                       ("forwardx11", "no"), ("connecttimeout", "10")):
        assert values[key] == [value], key
    # ControlPath=none leaves no control socket for ssh to print.
    assert "controlpath" not in values


# ---------------------------------------------------------------- connect


def enrolled(run_dir: Path, line: str | None = None, code: int = 0):
    """A run of the enrollment script that writes ``line`` to known_hosts and exits ``code``."""
    def answer(argv, options):
        assert argv == [str(clone_guest.ENROLL_SCRIPT), "--host", "127.0.0.1", "--port", str(PORT),
                        "--expected-fingerprint", channel.fingerprint(blob()), "--out", str(run_dir / "known_hosts")]
        assert options == {"timeout": clone_guest.ENROLL_TIMEOUT}
        if code == 0:
            path = run_dir / "known_hosts"
            path.write_text(line if line is not None else f"[127.0.0.1]:{PORT} ssh-ed25519 {encoded(blob())}\n",
                            encoding="ascii")
            path.chmod(0o600)
        return code, ""
    return answer


class Instance:
    """The enrollment script, then ssh, in the order ``connect`` and ``check_runner_units`` call them."""

    def __init__(self, run_dir: Path, enrollments: list, logins: list, queries: list | None = None) -> None:
        self.run_dir, self.enrollments, self.logins, self.queries = run_dir, enrollments, logins, list(queries or [])
        self.commands: list[str] = []
        self.sleeps: list[float] = []

    def __call__(self, argv, **options):
        if argv[0] != "ssh":
            return self.enrollments.pop(0)(argv, options)
        self.commands.append(argv[-1])
        assert options["timeout"] == clone_guest.QUERY_TIMEOUT
        return (self.logins if argv[-1] == "id -u" else self.queries).pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_connect_waits_for_the_host_key_and_the_login_then_returns_a_root_session(tmp_path):
    run_dir = private_dir(tmp_path / "run")
    instance = Instance(run_dir, [enrolled(run_dir, code=2), enrolled(run_dir)], [(255, ""), (0, "0\n")],
                        [(0, UNITS), (0, UNITS)])
    session = channel.connect(guest(run_dir), instance, instance.sleep)
    assert instance.sleeps == [clone_guest.ENROLL_INTERVAL, channel.AUTH_INTERVAL]
    channel.check_runner_units(session)
    assert instance.commands == ["id -u", "id -u", *(shlex.join(query) for query in RUNNER_UNIT_QUERIES)]


def test_connect_refuses_an_existing_known_hosts_file(tmp_path):
    run_dir = private_dir(tmp_path / "run")
    (run_dir / "known_hosts").symlink_to(tmp_path / "nowhere")
    with pytest.raises(LaneBoxError, match="already has a known_hosts"):
        channel.connect(guest(run_dir), Recorder(), lambda _: None)


def test_connect_gives_up_when_the_host_key_never_matches(tmp_path):
    run_dir = private_dir(tmp_path / "run")
    instance = Instance(run_dir, [enrolled(run_dir, code=2)] * clone_guest.ENROLL_ATTEMPTS, [])
    with pytest.raises(LaneBoxError, match="did not become ready or does not match"):
        channel.connect(guest(run_dir), instance, instance.sleep)
    assert len(instance.sleeps) == clone_guest.ENROLL_ATTEMPTS and instance.commands == []


@pytest.mark.parametrize("line", [
    f"[127.0.0.1]:{PORT} ssh-ed25519 {encoded(blob(b'o'))}\n",
    f"[127.0.0.1]:{PORT + 1} ssh-ed25519 {encoded(blob())}\n",
    f"127.0.0.1 ssh-ed25519 {encoded(blob())}\n",
    f"[127.0.0.1]:{PORT} ssh-ed25519 {encoded(blob())}\n" * 2,
    f"[127.0.0.1]:{PORT} ssh-ed25519 {encoded(blob())} comment\n",
])
def test_connect_checks_the_enrolled_key_itself(tmp_path, line):
    run_dir = private_dir(tmp_path / "run")
    instance = Instance(run_dir, [enrolled(run_dir, line)], [])
    with pytest.raises(LaneBoxError, match="not the box receipt's host key"):
        channel.connect(guest(run_dir), instance, instance.sleep)
    assert instance.commands == []


@pytest.mark.parametrize("logins, message", [
    ([(255, "")] * channel.AUTH_ATTEMPTS, "exit code 255"),
    ([(124, "")], "exit code 124"),
    ([(0, "1000\n")], "not user ID 0"),
])
def test_connect_refuses_a_login_that_is_not_root(tmp_path, logins, message):
    run_dir = private_dir(tmp_path / "run")
    instance = Instance(run_dir, [enrolled(run_dir)], list(logins))
    with pytest.raises(LaneBoxError, match=message):
        channel.connect(guest(run_dir), instance, instance.sleep)
    assert len(instance.commands) == len(logins)


# ---------------------------------------------------------------- runner units


def session_with(queries: list) -> channel.Session:
    def run(argv, **options):
        return queries.pop(0)
    return channel.Session(guest(Path("/run/lane")), run)


def test_an_instance_with_a_runner_unit_stops_the_run():
    runner = "actions.runner.org-repo.lane.service loaded active running\n"
    with pytest.raises(LaneBoxError, match="actions.runner.org-repo.lane.service"):
        channel.check_runner_units(session_with([(0, UNITS), (0, UNITS + runner)]))


@pytest.mark.parametrize("results", [[(1, UNITS), (0, UNITS)], [(0, UNITS), (0, "")], [(0, UNITS), (255, "")]])
def test_a_runner_unit_query_that_fails_stops_the_run(results):
    with pytest.raises(LaneBoxError):
        channel.check_runner_units(session_with(list(results)))
