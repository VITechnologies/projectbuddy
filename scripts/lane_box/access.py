"""Lane access (VITechnologies/projectbuddy#206): key-only root SSH from the host, for the next boot.

``configure-access`` installs the files under ``guest/`` and enables the unit; the readiness scan attests them.
At each boot of a lane instance, the unit writes root's only authorized key from a guest property that the
host sets before the VM starts, under /VirtualBox/HostInfo/, where VirtualBox refuses every guest write.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import stat
from dataclasses import dataclass

from .common import ChangedError, LaneBoxError
from .toolchain import PROBE_TIMEOUT, Runner

GUEST_FILES = pathlib.Path(__file__).resolve().parent / "guest"
ROOT_UID = 0
FILE_MODE = 0o644
DIRECTORY_MODE = 0o755
SSHD = "/usr/sbin/sshd"
NAT_GATEWAY = "10.0.2.2"
SSHD_DROP_IN_DIRECTORY = "etc/ssh/sshd_config.d"
UNIT_NAME = "projectbuddy-lane-access.service"
UNIT_LINK = f"etc/systemd/system/multi-user.target.wants/{UNIT_NAME}"
UNIT_LINK_TARGET = f"/etc/systemd/system/{UNIT_NAME}"
PASSWD = "etc/passwd"
NOLOGIN_SHELLS = frozenset({"/usr/sbin/nologin", "/sbin/nologin", "/usr/bin/false", "/bin/false"})
ACCOUNT_NAME = re.compile(r"[A-Za-z0-9._-]+\$?")
# `sshd -T` prints these settings with these values for every account that can log in. It prints
# `PermitRootLogin prohibit-password` under its older name, `without-password`.
SSHD_EXPECTED = {
    "allowusers": ["root"],
    "authenticationmethods": ["publickey"],
    "authorizedkeyscommand": ["none"],
    "authorizedkeysfile": ["/run/projectbuddy-ssh/%u"],
    "authorizedprincipalsfile": ["none"],
    "disableforwarding": ["yes"],
    "gssapiauthentication": ["no"],
    "hostbasedauthentication": ["no"],
    "kbdinteractiveauthentication": ["no"],
    "passwordauthentication": ["no"],
    "permitrootlogin": ["without-password"],
    "permittty": ["no"],
    "pubkeyacceptedalgorithms": ["ssh-ed25519"],
    "pubkeyauthentication": ["yes"],
    "trustedusercakeys": ["none"],
}


@dataclass(frozen=True)
class AccessFile:
    path: str  # relative to the guest root
    source: str  # file name under guest/

    def content(self) -> bytes:
        return (GUEST_FILES / self.source).read_bytes()

    def sha256(self) -> str:
        return hashlib.sha256(self.content()).hexdigest()


ACCESS_FILES = (
    AccessFile(f"{SSHD_DROP_IN_DIRECTORY}/00-projectbuddy-lane.conf", "00-projectbuddy-lane.conf"),
    AccessFile(f"etc/systemd/system/{UNIT_NAME}", UNIT_NAME),
    AccessFile("usr/local/libexec/projectbuddy-lane-access.py", "lane_access.py"),
)


def _write_new(path: pathlib.Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, FILE_MODE)


def configure_access(root: pathlib.Path) -> list[str]:
    """Install the access files and enable the unit for the next boot; return what was installed.

    Neither sshd nor the unit is restarted, so the running sshd keeps its configuration until the guest
    reboots. Raises ``LaneBoxError`` when nothing was installed and ``ChangedError`` once anything was.
    """
    if not (root / SSHD_DROP_IN_DIRECTORY).is_dir():
        raise LaneBoxError(f"/{SSHD_DROP_IN_DIRECTORY} is missing; is openssh-server installed?")
    targets = [item.path for item in ACCESS_FILES] + [UNIT_LINK]
    taken = ["/" + path for path in targets if (root / path).exists() or (root / path).is_symlink()]
    if taken:
        raise LaneBoxError(f"refusing to replace existing files: {', '.join(taken)}")
    changed: list[str] = []
    try:
        contents = [item.content() for item in ACCESS_FILES]
        for item, content in zip(ACCESS_FILES, contents):
            path = root / item.path
            path.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            _write_new(path, content)
            changed.append("/" + item.path)
        link = root / UNIT_LINK
        link.parent.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        link.symlink_to(UNIT_LINK_TARGET)
        changed.append("/" + UNIT_LINK)
    except OSError as error:
        if not changed:
            raise LaneBoxError(f"cannot configure lane access; no file was installed: {error}") from error
        raise ChangedError(f"{error}; the guest was already changed by: {', '.join(changed)}") from error
    return changed


def _file_state(root: pathlib.Path, item: AccessFile, errors: list[str]) -> dict:
    state = {"path": "/" + item.path, "expectedSha256": item.sha256(), "sha256": None, "mode": None, "uid": None}
    path = root / item.path
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            state.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         mode=f"{stat.S_IMODE(info.st_mode):04o}", uid=info.st_uid)
    except FileNotFoundError:
        pass
    except OSError as error:
        errors.append(f"/{item.path}: {error.strerror}")
    return state


def _accounts(root: pathlib.Path, errors: list[str]) -> tuple[list[str], list[str]]:
    """Return the accounts that can log in (always including root) and every account's home directory."""
    users, homes = {"root"}, {"/root"}
    try:
        lines = (root / PASSWD).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        errors.append(f"/{PASSWD}: {error}")
        lines = []
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split(":")
        if len(fields) != 7 or not ACCOUNT_NAME.fullmatch(fields[0]) or not fields[5].startswith("/"):
            errors.append(f"/{PASSWD}:{number}: the entry cannot be checked")
            continue
        homes.add(fields[5])
        if fields[6] not in NOLOGIN_SHELLS:
            users.add(fields[0])
    return sorted(users), sorted(homes)


def _sshd_effective(user: str, run: Runner) -> dict:
    code, stdout = run([SSHD, "-T", "-C", f"user={user},host={NAT_GATEWAY},addr={NAT_GATEWAY}"],
                       timeout=PROBE_TIMEOUT)
    effective: dict[str, list[str]] = {}
    if code == 0:
        effective = {key: [] for key in SSHD_EXPECTED}
        for line in stdout.splitlines():
            key, _, value = line.partition(" ")
            if key in effective:
                effective[key].append(value)
    return {"user": user, "exitCode": code, "effective": effective}


def _authorized_keys(root: pathlib.Path, homes: list[str], errors: list[str]) -> list[str]:
    found: list[str] = []
    for home in homes:
        directory = root / home.lstrip("/") / ".ssh"
        try:
            names = os.listdir(directory)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            errors.append(f"{home.rstrip('/')}/.ssh: {error.strerror}")
            continue
        found.extend(f"{home.rstrip('/')}/.ssh/{name}" for name in sorted(names) if name.startswith("authorized_keys"))
    return found


def scan_access(root: pathlib.Path, run: Runner) -> dict:
    errors: list[str] = []
    files = [_file_state(root, item, errors) for item in ACCESS_FILES]
    try:
        target = os.readlink(root / UNIT_LINK)
    except FileNotFoundError:
        target = None
    except OSError as error:
        errors.append(f"/{UNIT_LINK}: {error.strerror}")
        target = None
    users, homes = _accounts(root, errors)
    return {
        "files": files,
        "unitLink": {"path": "/" + UNIT_LINK, "target": target},
        "sshd": [_sshd_effective(user, run) for user in users],
        "authorizedKeys": _authorized_keys(root, homes, errors),
        "scanErrors": errors,
    }


def access_failures(scan: dict) -> list[str]:
    failures = [f"lane access file is missing or differs: {state['path']}" for state in scan["files"]
                if (state["sha256"], state["mode"], state["uid"]) != (state["expectedSha256"], f"{FILE_MODE:04o}", ROOT_UID)]
    if scan["unitLink"]["target"] != UNIT_LINK_TARGET:
        failures.append(f"{UNIT_NAME} is not enabled")
    for entry in scan["sshd"]:
        if entry["exitCode"] != 0:
            failures.append(f"sshd -T failed for {entry['user']} with exit code {entry['exitCode']}")
        elif entry["effective"] != SSHD_EXPECTED:
            differing = sorted(key for key in SSHD_EXPECTED if entry["effective"][key] != SSHD_EXPECTED[key])
            failures.append(f"sshd does not enforce lane access for {entry['user']}: {', '.join(differing)}")
    if scan["authorizedKeys"]:
        failures.append("an authorized_keys file is present: " + ", ".join(scan["authorizedKeys"]))
    if scan["scanErrors"]:
        failures.append("the lane access scan was incomplete")
    return failures


def expected_files() -> list[dict]:
    """The ``files`` block of a passing scan."""
    return [{"path": "/" + item.path, "expectedSha256": item.sha256(), "sha256": item.sha256(),
             "mode": f"{FILE_MODE:04o}", "uid": ROOT_UID} for item in ACCESS_FILES]
