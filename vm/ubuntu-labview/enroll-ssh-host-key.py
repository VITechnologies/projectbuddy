#!/usr/bin/env python3
import argparse
import os
import pathlib
import re
import subprocess
import sys
import tempfile


HOST_PATTERN = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|[0-9A-Fa-f:]+)")
FINGERPRINT_PATTERN = re.compile(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}")


def fail(message: str) -> int:
    print(f"SSH host-key enrollment: FAIL: {message}", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--expected-fingerprint", required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--keyscan-executable", default="ssh-keyscan")
    arguments = parser.parse_args()

    if not HOST_PATTERN.fullmatch(arguments.host) or arguments.host.startswith("-"):
        return fail("host is invalid")
    if not 1 <= arguments.port <= 65535:
        return fail("port must be from 1 through 65535")
    if not FINGERPRINT_PATTERN.fullmatch(arguments.expected_fingerprint):
        return fail("expected fingerprint must be an OpenSSH SHA-256 fingerprint")
    if arguments.out.exists():
        return fail("output already exists; verify identity before replacing it")

    try:
        scan = subprocess.run(
            [
                arguments.keyscan_executable, "-T", "10", "-p", str(arguments.port),
                "-t", "ed25519", arguments.host,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return fail(f"cannot run ssh-keyscan: {error}")
    if scan.returncode != 0:
        return fail("ssh-keyscan did not return a host key")

    key_lines = [
        line.strip() for line in scan.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(key_lines) != 1 or " ssh-ed25519 " not in key_lines[0]:
        return fail("expected exactly one ED25519 host key")

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=arguments.out.parent,
            prefix=f".{arguments.out.name}.",
            delete=False,
        ) as temporary:
            temporary.write(key_lines[0] + "\n")
            temporary_path = pathlib.Path(temporary.name)
        temporary_path.chmod(0o600)
        try:
            fingerprint = subprocess.run(
                ["ssh-keygen", "-lf", str(temporary_path), "-E", "sha256"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            return fail(f"cannot run ssh-keygen: {error}")
        if fingerprint.returncode != 0:
            return fail("ssh-keygen could not fingerprint the collected key")
        fingerprints = FINGERPRINT_PATTERN.findall(fingerprint.stdout)
        if fingerprints != [arguments.expected_fingerprint]:
            return fail("collected key does not match the expected fingerprint")
        os.replace(temporary_path, arguments.out)
        temporary_path = None
        arguments.out.chmod(0o600)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    print(f"SSH host-key enrollment: PASS: {arguments.host}:{arguments.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())