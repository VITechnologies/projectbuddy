"""The lane's output disk (REQ-106, completion channel C1): its format, and the launcher that writes it.

The disk holds one POSIX ustar archive from byte 0, with no file system, then zeros. Its first member,
``./build-result``, holds the build unit's result; the declared outputs that exist follow in declared
order. Every header is the one :func:`header` makes, so the host accepts exactly what this module writes.

The launcher runs as root in the guest after the build unit has ended and its control group is empty. It
uses the standard library only, so the engine can copy this one file into the guest and run it with the
guest's ``python3``, which ``base-bootstrap.sh`` installs.
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from typing import Callable, Mapping, Sequence

BLOCK = 512
MIB = 1024 ** 2
RESULT_MEMBER = "./build-result"
NAME_LIMIT = 100
SIZE_LIMIT = 8 ** 11
RESULT_LIMIT = 16
RESULT = re.compile(r"exited (0|[1-9][0-9]{0,2})|killed ([1-9][0-9]?)|timeout")
EXIT_LIMIT = 255
SIGNAL_LIMIT = 64
# systemd's ExecMainCode is the waitid() si_code of the unit's main process.
CLD_EXITED, CLD_KILLED, CLD_DUMPED = 1, 2, 3
UNIT_NAME = re.compile(r"[A-Za-z0-9:_.@-]+\.service")
UNIT_PROPERTIES = ("Result", "ExecMainCode", "ExecMainStatus")
LANE_GUEST_MARKER = "/var/lib/projectbuddy/base-bootstrap-state.json"
POWEROFF = ("systemctl", "poweroff")
CHUNK = MIB

Run = Callable[..., "subprocess.CompletedProcess[str]"]


def padded(size: int) -> int:
    """``size`` rounded up to whole blocks."""
    return -(-size // BLOCK) * BLOCK


def archive_length(sizes: Sequence[int]) -> int:
    """The bytes an archive of members of ``sizes`` takes: a header and padded data each, then two zero blocks."""
    return sum(BLOCK + padded(size) for size in sizes) + 2 * BLOCK


SMALLEST_ARCHIVE = archive_length([len("exited 255\n")])


def disk_bytes(max_output_bytes: int) -> int:
    """The output disk's size: ``maxOutputBytes`` rounded up to whole MiB."""
    return -(-max_output_bytes // MIB) * MIB


def header(name: str, size: int) -> bytes:
    """The one ustar header the launcher writes, and the host accepts, for member ``name`` of ``size`` bytes.

    A regular file, mode 0600, owner and group 0, modification time 0, no user or group name, no prefix.
    """
    encoded = name.encode("utf-8")
    if not encoded or len(encoded) > NAME_LIMIT or b"\0" in encoded:
        raise ValueError(f"member name {name!r} must be 1 to {NAME_LIMIT} UTF-8 bytes without NUL")
    if not 0 <= size < SIZE_LIMIT:
        raise ValueError(f"member size {size} must be below {SIZE_LIMIT}")
    block = bytearray(BLOCK)
    block[0:len(encoded)] = encoded
    block[100:108] = b"0000600\0"
    block[108:116] = b"0000000\0"
    block[116:124] = b"0000000\0"
    block[124:136] = b"%011o\0" % size
    block[136:148] = b"00000000000\0"
    block[148:156] = b" " * 8
    block[156:157] = b"0"
    block[257:263] = b"ustar\0"
    block[263:265] = b"00"
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def parse_result(data: bytes) -> str | None:
    """The build result that ``data`` holds as one line, or None."""
    if not data.endswith(b"\n"):
        return None
    try:
        text = data[:-1].decode("ascii")
    except UnicodeDecodeError:
        return None
    match = RESULT.fullmatch(text)
    if match is None:
        return None
    if match.group(1) is not None and int(match.group(1)) > EXIT_LIMIT:
        return None
    if match.group(2) is not None and int(match.group(2)) > SIGNAL_LIMIT:
        return None
    return text


def result_from_unit(properties: Mapping[str, str]) -> str | None:
    """The build result for a unit's ``Result``, ``ExecMainCode``, and ``ExecMainStatus``, or None."""
    if properties.get("Result") == "timeout":
        return "timeout"
    code, status = properties.get("ExecMainCode"), properties.get("ExecMainStatus", "")
    if not status.isdigit() or not status.isascii():
        return None
    if code == str(CLD_EXITED):
        return parse_result(f"exited {int(status)}\n".encode())
    if code in (str(CLD_KILLED), str(CLD_DUMPED)):
        return parse_result(f"killed {int(status)}\n".encode())
    return None


def unit_properties(unit: str, run: Run) -> dict[str, str]:
    """The build unit's result properties, read with ``systemctl show``; empty when they cannot be read."""
    argv = ["systemctl", "show", *(f"--property={name}" for name in UNIT_PROPERTIES), "--", unit]
    completed = run(argv, capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode != 0:
        return {}
    found: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if not separator or name not in UNIT_PROPERTIES or name in found:
            return {}
        found[name] = value
    return found


def open_output(root: str, relative: str, uid: int) -> tuple[int, int] | None:
    """A read descriptor and the size of one declared output, or None.

    Every component of ``root`` and ``relative`` is opened without following a symbolic link. The
    output must be a regular file that ``uid`` owns and that has one link, so a build step cannot make
    the root launcher read a file it could not read itself.
    """
    parts = [part for part in root.split("/") if part] + relative.split("/")
    if not root.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return None
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
    except OSError:
        return None
    finally:
        os.close(directory)
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_uid != uid or status.st_nlink != 1 \
            or status.st_size >= SIZE_LIMIT:
        os.close(descriptor)
        return None
    return descriptor, status.st_size


def _write_all(device: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(device, view):]


def _copy(source: int, device: int, size: int) -> None:
    remaining = size
    while remaining:
        data = os.read(source, min(remaining, CHUNK))
        if not data:
            raise OSError(f"an output ended {remaining} bytes early")
        _write_all(device, data)
        remaining -= len(data)


def write_disk(device: int, result: str | None, members: Sequence[tuple[str, int, int]],
               max_output_bytes: int) -> list[str]:
    """Write the archive from byte 0 of ``device``, zero the rest, and flush it; return the outputs written.

    ``members`` are (name, descriptor, size). With no result, or when not even the result fits in
    ``max_output_bytes``, nothing is archived. When the result and every member do not fit, the result
    is archived alone. The first header is written last, after everything else is flushed: until then
    block 0 is zero and block 1 is not, so a disk the launcher left partway is one the host refuses.
    """
    end = os.lseek(device, 0, os.SEEK_END)
    os.lseek(device, 0, os.SEEK_SET)
    written: list[str] = []
    data = b"" if result is None else f"{result}\n".encode("ascii")
    first = None
    if result is not None and archive_length([len(data)]) <= max_output_bytes:
        if archive_length([len(data), *(size for _, _, size in members)]) > max_output_bytes:
            members = []
        first = header(RESULT_MEMBER, len(data))
        _write_all(device, bytes(BLOCK) + data.ljust(padded(len(data)), b"\0"))
        for name, descriptor, size in members:
            _write_all(device, header(name, size))
            _copy(descriptor, device, size)
            _write_all(device, bytes(padded(size) - size))
            written.append(name)
        _write_all(device, bytes(2 * BLOCK))
    position = os.lseek(device, 0, os.SEEK_CUR)
    while position < end:
        count = min(CHUNK, end - position)
        _write_all(device, bytes(count))
        position += count
    os.fsync(device)
    if first is not None:
        os.lseek(device, 0, os.SEEK_SET)
        _write_all(device, first)
        os.fsync(device)
    return written


def launch(device: int, root: str, uid: int, outputs: Sequence[str], max_output_bytes: int,
           result: str | None) -> list[str]:
    """Archive ``result`` and the declared ``outputs`` found below ``root`` on ``device``."""
    members: list[tuple[str, int, int]] = []
    try:
        for name in outputs:
            opened = open_output(root, name, uid)
            if opened is not None:
                members.append((name, *opened))
        return write_disk(device, result, members, max_output_bytes)
    finally:
        for _, descriptor, _ in members:
            os.close(descriptor)


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write the lane output disk, then power the guest off.")
    parser.add_argument("--device", required=True, help="the output disk's block device")
    parser.add_argument("--root", required=True, help="the absolute working directory the outputs are below")
    parser.add_argument("--uid", required=True, type=int, help="the workload account's user ID")
    parser.add_argument("--unit", required=True, help="the build unit, kept loaded after it ends")
    parser.add_argument("--max-output-bytes", required=True, type=int)
    parser.add_argument("--output", action="append", default=[], help="a declared output, in declared order")
    return parser.parse_args(argv)


def main(argv: Sequence[str], run: Run = subprocess.run, marker: str = LANE_GUEST_MARKER) -> int:
    """Outside a lane guest, change nothing and return 2. In one, write the disk, then always power off."""
    if not os.path.isfile(marker):
        parse_arguments(argv)
        print(f"not a lane guest: {marker} is missing; nothing was written", file=sys.stderr)
        return 2
    code = 1
    try:
        try:
            arguments = parse_arguments(argv)
        except SystemExit:
            return code
        if not UNIT_NAME.fullmatch(arguments.unit) or arguments.max_output_bytes < SMALLEST_ARCHIVE:
            print("the unit name or maxOutputBytes is not valid; nothing was written", file=sys.stderr)
            return code
        result = result_from_unit(unit_properties(arguments.unit, run))
        device = os.open(arguments.device, os.O_WRONLY | os.O_CLOEXEC)
        try:
            size = os.lseek(device, 0, os.SEEK_END)
            if not stat.S_ISBLK(os.fstat(device).st_mode) or size != disk_bytes(arguments.max_output_bytes):
                print(f"{arguments.device} is not a block device of {disk_bytes(arguments.max_output_bytes)} "
                      "bytes; nothing was written", file=sys.stderr)
                return code
            launch(device, arguments.root, arguments.uid, arguments.output, arguments.max_output_bytes, result)
            code = 0
        finally:
            os.close(device)
    finally:
        run(list(POWEROFF), check=False, timeout=60)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
