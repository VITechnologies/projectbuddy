"""Accept the lane run's SSH client key from the host (VITechnologies/projectbuddy#206).

``Invoke-LaneBox.py configure-access`` installs this file as /usr/local/libexec/projectbuddy-lane-access.py, and
projectbuddy-lane-access.service runs it as root once per boot. It reads the guest property
/VirtualBox/HostInfo/ProjectBuddy/Lane/ClientKey. VirtualBox refuses every guest write under /VirtualBox/HostInfo/,
so only the host can have set it; a property under any other name could have been created by a guest process,
with flags of its choosing. When the property is read-only for the guest and holds exactly one ssh-ed25519 public
key, that key becomes root's only authorized key, usable only from the NAT gateway. Anything else leaves root with
no key, so every SSH login fails.

Standard library only; it must not import the lane_box package, which is deleted from the guest.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
import sys
import tempfile

PROPERTY = "/VirtualBox/HostInfo/ProjectBuddy/Lane/ClientKey"
VBOXCONTROL = "/usr/sbin/VBoxControl"
KEY_DIRECTORY = "/run/projectbuddy-ssh"
NAT_GATEWAY = "10.0.2.2"
TIMEOUT = 30
# The wire form of an ssh-ed25519 public key: the length-prefixed name, then the length-prefixed 32-byte key.
BLOB_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
SHOWN = re.compile(r"Value: (?P<value>[^\n]*)\nTimestamp: [0-9]+ ns\nFlags: (?P<flags>[^\n]*)\n")
KEY = re.compile(r"ssh-ed25519 (?P<blob>[A-Za-z0-9+/]{68})(?: [!-~]{1,255})?")
GUEST_READ_ONLY = frozenset({"RDONLYGUEST", "READONLY"})


def authorized_line(shown: str) -> tuple[str, str]:
    """Return root's authorized_keys line and the key's fingerprint, or raise ``ValueError``."""
    match = SHOWN.fullmatch(shown)
    if not match:
        raise ValueError("VBoxControl did not print exactly one property")
    if not {flag.strip() for flag in match["flags"].split(",")} & GUEST_READ_ONLY:
        raise ValueError(f"{PROPERTY} is writable by the guest")
    key = KEY.fullmatch(match["value"])
    if not key:
        raise ValueError(f"{PROPERTY} is not exactly one ssh-ed25519 public key")
    blob = base64.b64decode(key["blob"], validate=True)
    if not blob.startswith(BLOB_PREFIX):
        raise ValueError(f"{PROPERTY} is not an ssh-ed25519 public key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f'restrict,from="{NAT_GATEWAY}" ssh-ed25519 {key["blob"]}\n', fingerprint


def install(shown: str, directory: str) -> str:
    """Write root's key file with mode 644 in a mode 755 directory and return the fingerprint."""
    line, fingerprint = authorized_line(shown)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    os.chmod(directory, 0o755)
    descriptor, temporary = tempfile.mkstemp(prefix=".root.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(line)
        os.chmod(temporary, 0o644)
        os.replace(temporary, os.path.join(directory, "root"))
    except BaseException:
        os.unlink(temporary)
        raise
    return fingerprint


def refuse(reason: str) -> int:
    print(f"projectbuddy-lane-access: no client key installed: {reason}", file=sys.stderr)
    return 1


def main(vboxcontrol: str = VBOXCONTROL, directory: str = KEY_DIRECTORY) -> int:
    try:
        os.unlink(os.path.join(directory, "root"))
    except FileNotFoundError:
        pass
    except OSError as error:
        return refuse(f"cannot remove the earlier key: {error}")
    try:
        completed = subprocess.run(
            [vboxcontrol, "--nologo", "guestproperty", "get", PROPERTY, "--verbose"],
            capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return refuse(f"cannot run VBoxControl: {error}")
    if completed.returncode != 0:
        return refuse(f"{PROPERTY} is not set (VBoxControl exit code {completed.returncode})")
    try:
        fingerprint = install(completed.stdout, directory)
    except (OSError, ValueError) as error:
        return refuse(str(error))
    print(f"projectbuddy-lane-access: root accepts only {fingerprint}, from {NAT_GATEWAY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
