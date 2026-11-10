"""Read the lane's output disk on the host after ``poweroff`` (REQ-106, completion channel C1).

The engine streams the disk with ``VBoxManage mediumio --disk=<path> cat --output=-`` and passes the
stream here. Anything on the disk other than the archive :mod:`lane.output_disk` writes fails the run,
and a failed read keeps no output.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Sequence

from .common import Refusal
from .output_disk import BLOCK, CHUNK, RESULT_LIMIT, RESULT_MEMBER, header, padded, parse_result

READ_RULES = (
    "disk-length", "header", "member", "padding", "length", "end", "tail", "build-result", "output-missing",
)
ZERO_BLOCK = bytes(BLOCK)


@dataclass(frozen=True)
class Reading:
    """The build result, the outputs kept in the staging directory, and why the read failed, if it did.

    A disk that breaks the format has no result; one that lacks a declared output keeps its result.
    """

    result: str | None
    outputs: tuple[str, ...]
    refusals: tuple[Refusal, ...]


class _Refused(Exception):
    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(detail)
        self.refusal = Refusal(rule, detail)


class _Disk:
    """Exact reads from the stream, which must hold ``size`` bytes."""

    def __init__(self, stream: BinaryIO, size: int) -> None:
        self.stream, self.size, self.position = stream, size, 0

    def read(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            chunk = self.stream.read(min(count - len(data), CHUNK))
            if not chunk:
                raise _Refused("disk-length", f"the output disk ended after {self.position + len(data)} of "
                                              f"{self.size} bytes")
            data += chunk
        self.position += count
        return bytes(data)


def _member(block: bytes, index: int) -> tuple[str, int]:
    """The name and size of a header block that is exactly the header the launcher writes.

    A detail names a member by its position only: a name on the disk is the guest's text.
    """
    size_field = block[124:136]
    if not size_field.endswith(b"\0") or not all(48 <= byte <= 55 for byte in size_field[:-1]):
        raise _Refused("header", f"member {index}'s size is not eleven octal digits and a NUL")
    try:
        name = block[:100].split(b"\0", 1)[0].decode("utf-8")
        size = int(size_field[:-1], 8)
        expected = header(name, size)
    except (UnicodeDecodeError, ValueError):
        raise _Refused("header", f"member {index}'s name is not 1 to 100 bytes of UTF-8") from None
    if block != expected:
        raise _Refused("header", f"member {index}'s header differs from the one the launcher writes")
    return name, size


def _keep(staging: Path, name: str, disk: _Disk, size: int, created: list[Path]) -> None:
    target = staging / name
    for parent in reversed(target.relative_to(staging).parents[:-1]):
        directory = staging / parent
        if not directory.is_dir():
            directory.mkdir(mode=0o700)
            created.append(directory)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    created.append(target)
    with os.fdopen(descriptor, "wb") as handle:
        remaining = size
        while remaining:
            count = min(remaining, CHUNK)
            handle.write(disk.read(count))
            remaining -= count


def _remove(created: list[Path]) -> None:
    for path in reversed(created):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()


def read_disk(stream: BinaryIO, size: int, outputs: Sequence[str], max_output_bytes: int,
              staging: Path) -> Reading:
    """Check the whole disk in ``stream`` and copy the declared outputs into the empty ``staging`` directory.

    ``size`` is the output disk's size and ``outputs`` are the declared outputs in declared order. A
    refusal, or an error while staging, removes everything the read put in ``staging``.
    """
    disk = _Disk(stream, size)
    created: list[Path] = []
    result: str | None = None
    kept: list[str] = []
    refusals: list[Refusal] = []
    try:
        index = next_output = 0
        while True:
            block = disk.read(BLOCK)
            if block == ZERO_BLOCK:
                if disk.read(BLOCK) != ZERO_BLOCK:
                    raise _Refused("end", f"the zero block after member {index} is not followed by a second one")
                break
            index += 1
            name, member_size = _member(block, index)
            # The archive, with its two closing zero blocks, must still fit after this member.
            if disk.position + padded(member_size) + 2 * BLOCK > max_output_bytes:
                raise _Refused("length", f"member {index} runs past maxOutputBytes ({max_output_bytes})")
            if index == 1:
                if name != RESULT_MEMBER:
                    raise _Refused("member", f"member 1 is not {RESULT_MEMBER!r}")
                if member_size > RESULT_LIMIT:
                    raise _Refused("build-result", f"the build result has {member_size} bytes, more than "
                                                   f"{RESULT_LIMIT}")
                result = parse_result(disk.read(member_size))
                if result is None:
                    raise _Refused("build-result", "the build result is not one result line")
            elif name in outputs[next_output:]:
                next_output = outputs.index(name, next_output) + 1
                _keep(staging, name, disk, member_size, created)
                kept.append(name)
            else:
                raise _Refused("member", f"member {index} is not a declared output that follows the previous "
                                         "member in declared order")
            if any(disk.read(padded(member_size) - member_size)):
                raise _Refused("padding", f"the padding after member {index} is not zero")
        while disk.position < size:
            if any(disk.read(min(CHUNK, size - disk.position))):
                raise _Refused("tail", "a byte after the end of the archive is not zero")
        if stream.read(1):
            raise _Refused("disk-length", f"the output disk holds more than {size} bytes")
        if result is None:
            raise _Refused("build-result", "the output disk holds no build result")
    except _Refused as refused:
        refusals.append(refused.refusal)
        result = None
    except BaseException:
        _remove(created)
        raise
    else:
        refusals = [Refusal("output-missing", f"declared output {name!r} is not on the output disk")
                    for name in outputs if name not in kept]
    if refusals:
        _remove(created)
        kept = []
    return Reading(result=result, outputs=tuple(kept), refusals=tuple(refusals))
