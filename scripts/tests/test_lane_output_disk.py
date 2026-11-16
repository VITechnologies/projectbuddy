"""Ubuntu lane output disk (REQ-106, C1): the launcher's archive, its safety checks, and the host reader."""
from __future__ import annotations

import io
import os
import random
import runpy
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lane import declaration, output_disk, outputs  # noqa: E402
from lane.output_disk import BLOCK, MIB, RESULT_MEMBER, header  # noqa: E402

REPOSITORY = SCRIPTS.parent
EXAMPLE_DECLARATION = REPOSITORY / "vm" / "ubuntu-lane" / "lane-workload.example.json"
DISK = 64 * 1024
UID = os.getuid()


def tree(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def device(path: Path, size: int = DISK, fill: bytes = b"\xff") -> int:
    path.write_bytes(fill * size)
    return os.open(path, os.O_RDWR)


def written_disk(tmp_path: Path, files: dict[str, bytes], declared: list[str], result: str | None = "exited 0",
                 maximum: int = DISK) -> bytes:
    root = tree(tmp_path / "work", files)
    image = tmp_path / "disk.img"
    descriptor = device(image)
    try:
        output_disk.launch(descriptor, str(root), UID, declared, maximum, result)
    finally:
        os.close(descriptor)
    return image.read_bytes()


def archive(*members: tuple[str, bytes], size: int = DISK) -> bytearray:
    """A disk as the launcher would write it for ``members``, built without the launcher."""
    data = bytearray()
    for name, content in members:
        data += header(name, len(content)) + content + bytes(output_disk.padded(len(content)) - len(content))
    data += bytes(2 * BLOCK)
    return data + bytes(size - len(data))


def read(disk: bytes, declared: list[str], staging: Path, maximum: int = DISK, size: int = DISK,
         stream: io.RawIOBase | None = None) -> outputs.Reading:
    staging.mkdir(exist_ok=True)
    return outputs.read_disk(stream or io.BytesIO(disk), size, declared, maximum, staging)


def rules_of(reading: outputs.Reading) -> list[str]:
    return [refusal.rule for refusal in reading.refusals]


def staged(staging: Path) -> list[str]:
    return sorted(path.relative_to(staging).as_posix() for path in staging.rglob("*"))


class Trickle(io.RawIOBase):
    """A pipe-like stream that returns at most seven bytes per read."""

    def __init__(self, data: bytes) -> None:
        self.data, self.position = data, 0

    def readable(self) -> bool:
        return True

    def read(self, count: int = -1) -> bytes:
        chunk = self.data[self.position:self.position + min(count, 7)]
        self.position += len(chunk)
        return chunk


# The format


@pytest.mark.parametrize("name,size", [("./build-result", 9), ("a", 0), ("dist/package.tgz", 12345),
                                       ("é" * 50, 1), ("x" * 100, output_disk.SIZE_LIMIT - 1)])
def test_a_header_is_one_tarfile_reads_as_written(name, size):
    block = header(name, size)
    assert len(block) == BLOCK
    info = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
    assert (info.name, info.size, info.mode, info.uid, info.gid, info.mtime) == (name, size, 0o600, 0, 0, 0)
    assert (info.type, info.uname, info.gname, info.linkname) == (tarfile.REGTYPE, "", "", "")
    assert block[257:265] == b"ustar\x0000"


@pytest.mark.parametrize("name,size", [("", 0), ("x" * 101, 0), ("é" * 51, 0), ("a\0b", 0), ("a", -1),
                                       ("a", output_disk.SIZE_LIMIT)])
def test_a_header_refuses_what_ustar_cannot_hold(name, size):
    with pytest.raises(ValueError):
        header(name, size)


def test_the_smallest_archive_and_the_disk_size():
    assert output_disk.SMALLEST_ARCHIVE == 2048 == output_disk.archive_length([len("exited 255\n")])
    assert output_disk.archive_length([]) == 1024
    assert output_disk.archive_length([0, 1, 512, 513]) == 4 * BLOCK + 0 + 512 + 512 + 1024 + 1024
    assert output_disk.disk_bytes(1) == output_disk.disk_bytes(MIB) == MIB
    assert output_disk.disk_bytes(MIB + 1) == 2 * MIB


@pytest.mark.parametrize("text", ["exited 0", "exited 7", "exited 255", "killed 1", "killed 64", "timeout"])
def test_a_result_line_parses(text):
    assert output_disk.parse_result(f"{text}\n".encode()) == text


@pytest.mark.parametrize("data", [b"exited 256\n", b"exited 01\n", b"exited -1\n", b"exited\n", b"killed 0\n",
                                  b"killed 65\n", b"timeout", b"timeout\n\n", b"Timeout\n", b"timeout \n",
                                  b"exited 0\r\n", b"\xff\n", b"", b"\n", b"exited \xd9\xa3\n"])
def test_anything_else_is_no_result(data):
    assert output_disk.parse_result(data) is None


@pytest.mark.parametrize("properties,result", [
    ({"Result": "timeout", "ExecMainCode": "2", "ExecMainStatus": "15"}, "timeout"),
    ({"Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0"}, "exited 0"),
    ({"Result": "exit-code", "ExecMainCode": "1", "ExecMainStatus": "3"}, "exited 3"),
    ({"Result": "signal", "ExecMainCode": "2", "ExecMainStatus": "9"}, "killed 9"),
    ({"Result": "core-dump", "ExecMainCode": "3", "ExecMainStatus": "6"}, "killed 6"),
    ({"Result": "success", "ExecMainCode": "0", "ExecMainStatus": "0"}, None),
    ({"Result": "exit-code", "ExecMainCode": "1", "ExecMainStatus": "256"}, None),
    ({"Result": "signal", "ExecMainCode": "2", "ExecMainStatus": "0"}, None),
    ({"Result": "exit-code", "ExecMainCode": "1", "ExecMainStatus": ""}, None),
    ({"Result": "exit-code", "ExecMainCode": "1", "ExecMainStatus": "\u00b2"}, None),
    ({"Result": "exit-code", "ExecMainCode": "1"}, None),
    ({}, None),
])
def test_the_result_follows_the_unit(properties, result):
    assert output_disk.result_from_unit(properties) == result


def fake_run(stdout: str = "Result=success\nExecMainCode=1\nExecMainStatus=0\n", returncode: int = 0):
    calls: list[list[str]] = []

    def run(argv, **options):
        calls.append(list(argv))
        assert options["check"] is False and options["timeout"] == 60
        return subprocess.CompletedProcess(argv, returncode if argv[1] == "show" else 0, stdout, "")
    return run, calls


def test_the_unit_properties_come_from_systemctl_show():
    run, calls = fake_run()
    assert output_disk.unit_properties("lane-build.service", run) == {
        "Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0"}
    assert calls == [["systemctl", "show", "--property=Result", "--property=ExecMainCode",
                      "--property=ExecMainStatus", "--", "lane-build.service"]]


@pytest.mark.parametrize("stdout,returncode", [
    ("Result=success\n", 1),
    ("Result=success\nnot a property\n", 0),
    ("Result=success\nMainPID=0\n", 0),
    ("Result=success\nResult=timeout\n", 0),
])
def test_unreadable_unit_properties_are_none(stdout, returncode):
    run, _ = fake_run(stdout, returncode)
    assert output_disk.unit_properties("lane-build.service", run) == {}


# The launcher's safety checks


def test_an_output_owned_by_the_workload_account_opens(tmp_path):
    root = tree(tmp_path / "work", {"dist/a.tgz": b"abc"})
    opened = output_disk.open_output(str(root), "dist/a.tgz", UID)
    assert opened is not None
    descriptor, size = opened
    try:
        assert size == 3 and os.read(descriptor, 10) == b"abc"
    finally:
        os.close(descriptor)


def symlinked_final(tmp_path: Path) -> tuple[str, str, int]:
    root = tree(tmp_path / "work", {"secret": b"host key"})
    (root / "out").symlink_to(root / "secret")
    return str(root), "out", UID


def symlinked_directory(tmp_path: Path) -> tuple[str, str, int]:
    root = tree(tmp_path / "work", {"real/out": b"x"})
    (root / "dist").symlink_to(root / "real")
    return str(root), "dist/out", UID


def symlinked_root(tmp_path: Path) -> tuple[str, str, int]:
    tree(tmp_path / "real", {"out": b"x"})
    (tmp_path / "work").symlink_to(tmp_path / "real")
    return str(tmp_path / "work"), "out", UID


def fifo(tmp_path: Path) -> tuple[str, str, int]:
    root = tree(tmp_path / "work", {})
    os.mkfifo(root / "out")
    return str(root), "out", UID


def directory(tmp_path: Path) -> tuple[str, str, int]:
    root = tree(tmp_path / "work", {"out/x": b"x"})
    return str(root), "out", UID


def other_owner(tmp_path: Path) -> tuple[str, str, int]:
    return str(tree(tmp_path / "work", {"out": b"x"})), "out", UID + 1


def hard_linked(tmp_path: Path) -> tuple[str, str, int]:
    root = tree(tmp_path / "work", {"out": b"x"})
    os.link(root / "out", tmp_path / "elsewhere")
    return str(root), "out", UID


def missing(tmp_path: Path) -> tuple[str, str, int]:
    return str(tree(tmp_path / "work", {})), "out", UID


def too_large(tmp_path: Path) -> tuple[str, str, int]:
    root = tree(tmp_path / "work", {})
    with open(root / "out", "wb") as handle:
        handle.truncate(output_disk.SIZE_LIMIT)
    return str(root), "out", UID


def relative_root(tmp_path: Path) -> tuple[str, str, int]:
    return "work", "out", UID


def dotted_root(tmp_path: Path) -> tuple[str, str, int]:
    tree(tmp_path / "work", {"out": b"x"})
    return f"{tmp_path}/./work", "out", UID


def parent_output(tmp_path: Path) -> tuple[str, str, int]:
    tree(tmp_path / "work", {"out": b"x"})
    return str(tmp_path / "work" / "sub"), "../out", UID


def empty_component(tmp_path: Path) -> tuple[str, str, int]:
    return str(tree(tmp_path / "work", {"dist/out": b"x"})), "dist//out", UID


@pytest.mark.parametrize("arrange", [symlinked_final, symlinked_directory, symlinked_root, fifo, directory,
                                     other_owner, hard_linked, missing, too_large, relative_root, dotted_root,
                                     parent_output, empty_component])
def test_an_output_the_workload_could_not_have_written_alone_is_skipped(tmp_path, arrange):
    assert output_disk.open_output(*arrange(tmp_path)) is None


def test_the_archive_reads_back_with_tarfile(tmp_path):
    files = {"dist/package.tgz": os.urandom(5000), "report.txt": b"ok\n", "empty": b""}
    disk = written_disk(tmp_path, files, ["report.txt", "missing.txt", "dist/package.tgz", "empty"])
    assert len(disk) == DISK
    with tarfile.open(fileobj=io.BytesIO(disk), mode="r:") as archived:
        members = archived.getmembers()
        assert [member.name for member in members] == [RESULT_MEMBER, "report.txt", "dist/package.tgz", "empty"]
        assert archived.extractfile(RESULT_MEMBER).read() == b"exited 0\n"
        for member in members[1:]:
            assert archived.extractfile(member).read() == files[member.name]
        assert all((member.mode, member.uid, member.mtime, member.uname) == (0o600, 0, 0, "") for member in members)
    end = output_disk.archive_length([9, *(len(files[name]) for name in ("report.txt", "dist/package.tgz", "empty"))])
    assert disk[end - 2 * BLOCK:] == bytes(DISK - end + 2 * BLOCK)


@pytest.mark.parametrize("result", ["exited 0", "exited 255", "killed 9", "timeout"])
def test_every_result_form_round_trips(tmp_path, result):
    reading = read(written_disk(tmp_path, {}, [], result), [], tmp_path / "staging")
    assert reading == outputs.Reading(result=result, outputs=(), refusals=())


def test_no_result_leaves_a_zero_disk(tmp_path):
    disk = written_disk(tmp_path, {"out": b"x"}, ["out"], result=None)
    assert disk == bytes(DISK)
    reading = read(disk, ["out"], tmp_path / "staging")
    assert rules_of(reading) == ["build-result"] and reading.result is None


def test_outputs_that_do_not_fit_leave_the_result_alone(tmp_path):
    disk = written_disk(tmp_path, {"out": b"x" * 600}, ["out"], maximum=output_disk.archive_length([9, 600]) - 1)
    assert disk[:output_disk.SMALLEST_ARCHIVE] == bytes(archive((RESULT_MEMBER, b"exited 0\n")))[:2048]
    reading = read(disk, ["out"], tmp_path / "staging", maximum=4096)
    assert reading.result == "exited 0" and rules_of(reading) == ["output-missing"]
    assert staged(tmp_path / "staging") == []


def test_a_maximum_below_the_smallest_archive_writes_nothing(tmp_path):
    assert written_disk(tmp_path, {}, [], maximum=output_disk.SMALLEST_ARCHIVE - 1) == bytes(DISK)


def test_a_launcher_stopped_partway_leaves_a_disk_the_host_refuses(tmp_path):
    source = tmp_path / "short"
    source.write_bytes(b"x" * 10)
    image = tmp_path / "disk.img"
    descriptor, member = device(image, fill=b"\0"), os.open(source, os.O_RDONLY)
    try:
        with pytest.raises(OSError, match="ended 90 bytes early"):
            output_disk.write_disk(descriptor, "exited 0", [("out", member, 100)], DISK)
    finally:
        os.close(member)
        os.close(descriptor)
    disk = image.read_bytes()
    assert disk[:BLOCK] == bytes(BLOCK) and disk[BLOCK:BLOCK + 9] == b"exited 0\n"
    reading = read(disk, ["out"], tmp_path / "staging")
    assert rules_of(reading) == ["end"] and reading.result is None


def test_the_first_header_is_written_after_everything_else_is_flushed(tmp_path, monkeypatch):
    image = tmp_path / "disk.img"
    descriptor = device(image)
    events: list[tuple[str, bytes]] = []
    real_fsync, real_write = os.fsync, os.write

    def fsync(fd):
        events.append(("fsync", b""))
        real_fsync(fd)

    def write(fd, data):
        events.append(("write", bytes(data[:BLOCK])))
        return real_write(fd, data)
    monkeypatch.setattr(output_disk.os, "fsync", fsync)
    monkeypatch.setattr(output_disk.os, "write", write)
    try:
        output_disk.write_disk(descriptor, "exited 0", [], DISK)
    finally:
        os.close(descriptor)
    assert events[-3:] == [("fsync", b""), ("write", header(RESULT_MEMBER, 9)), ("fsync", b"")]
    assert all(data != header(RESULT_MEMBER, 9) for _, data in events[:-2])


# The launcher's entry point


def launcher_arguments(device_path: Path, root: Path, *extra: str, unit: str = "lane-build.service",
                       maximum: int = MIB) -> list[str]:
    return ["--device", str(device_path), "--root", str(root), "--uid", str(UID), "--unit", unit,
            "--max-output-bytes", str(maximum), *extra]


@pytest.fixture
def marker(tmp_path):
    path = tmp_path / "base-bootstrap-state.json"
    path.write_text("{}", encoding="utf-8")
    return str(path)


def test_outside_a_lane_guest_the_launcher_changes_nothing(tmp_path, capsys):
    image = tmp_path / "disk.img"
    image.write_bytes(b"\xff" * MIB)
    run, calls = fake_run()
    code = output_disk.main(launcher_arguments(image, tmp_path), run=run, marker=str(tmp_path / "absent"))
    assert code == 2 and calls == [] and image.read_bytes() == b"\xff" * MIB
    assert "not a lane guest" in capsys.readouterr().err


@pytest.mark.parametrize("unit,maximum", [("lane-build", MIB), ("../x.service", MIB), ("a b.service", MIB),
                                          ("lane-build.service", output_disk.SMALLEST_ARCHIVE - 1)])
def test_invalid_arguments_write_nothing_and_still_power_off(tmp_path, marker, unit, maximum):
    image = tmp_path / "disk.img"
    image.write_bytes(b"\xff" * MIB)
    run, calls = fake_run()
    code = output_disk.main(launcher_arguments(image, tmp_path, unit=unit, maximum=maximum), run=run, marker=marker)
    assert code == 1 and calls == [list(output_disk.POWEROFF)] and image.read_bytes() == b"\xff" * MIB


@pytest.mark.parametrize("drop,extra", [(2, []), (0, ["--help"]), (0, ["--uid", ""]),
                                        (0, ["--max-output-bytes", "1e6"])],
                         ids=["no-device", "help", "empty-uid", "float-maximum"])
def test_malformed_arguments_in_a_lane_guest_write_nothing_and_still_power_off(tmp_path, marker, drop, extra):
    image = tmp_path / "disk.img"
    image.write_bytes(b"\xff" * MIB)
    run, calls = fake_run()
    assert output_disk.main(launcher_arguments(image, tmp_path, *extra)[drop:], run=run, marker=marker) == 1
    assert calls == [list(output_disk.POWEROFF)] and image.read_bytes() == b"\xff" * MIB


def test_malformed_arguments_outside_a_lane_guest_change_nothing(tmp_path):
    run, calls = fake_run()
    with pytest.raises(SystemExit) as stopped:
        output_disk.main(["--uid", "x"], run=run, marker=str(tmp_path / "absent"))
    assert stopped.value.code == 2 and calls == []


@pytest.mark.parametrize("block_device,size", [(False, MIB), (True, 2 * MIB)])
def test_a_device_that_is_not_the_output_disk_is_not_written(tmp_path, marker, monkeypatch, block_device, size):
    monkeypatch.setattr(output_disk.stat, "S_ISBLK", lambda mode: block_device)
    image = tmp_path / "disk.img"
    image.write_bytes(b"\xff" * size)
    run, calls = fake_run()
    assert output_disk.main(launcher_arguments(image, tmp_path), run=run, marker=marker) == 1
    assert calls[-1] == list(output_disk.POWEROFF) and image.read_bytes() == b"\xff" * size


def test_the_launcher_writes_the_disk_then_powers_off(tmp_path, marker, monkeypatch):
    monkeypatch.setattr(output_disk.stat, "S_ISBLK", stat.S_ISREG)
    root = tree(tmp_path / "work", {"dist/a.tgz": b"package", "log.txt": b"log"})
    image = tmp_path / "disk.img"
    image.write_bytes(b"\xff" * 2 * MIB)
    run, calls = fake_run("Result=exit-code\nExecMainCode=1\nExecMainStatus=4\n")
    arguments = launcher_arguments(image, root, "--output", "dist/a.tgz", "--output", "log.txt", maximum=MIB + 1)
    assert output_disk.main(arguments, run=run, marker=marker) == 0
    assert [call[1] for call in calls] == ["show", "poweroff"]
    reading = read(image.read_bytes(), ["dist/a.tgz", "log.txt"], tmp_path / "staging", maximum=MIB + 1,
                   size=2 * MIB)
    assert reading == outputs.Reading(result="exited 4", outputs=("dist/a.tgz", "log.txt"), refusals=())
    assert (tmp_path / "staging" / "dist" / "a.tgz").read_bytes() == b"package"


def test_the_launcher_powers_off_when_writing_fails(tmp_path, marker):
    run, calls = fake_run()
    with pytest.raises(FileNotFoundError):
        output_disk.main(launcher_arguments(tmp_path / "absent", tmp_path), run=run, marker=marker)
    assert calls[-1] == list(output_disk.POWEROFF)


def test_the_launcher_runs_as_a_script(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["output_disk.py", "--help"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(SCRIPTS / "lane" / "output_disk.py"), run_name="__main__")
    assert stopped.value.code == 0 and "--max-output-bytes" in capsys.readouterr().out


def test_the_launcher_needs_only_the_standard_library():
    source = (SCRIPTS / "lane" / "output_disk.py").read_text(encoding="utf-8")
    imported = {line.split()[1].split(".")[0] for line in source.splitlines()
                if line.startswith(("import ", "from ")) and not line.startswith("from __future__")}
    assert imported <= set(sys.stdlib_module_names), imported


# The host reader


def test_a_disk_the_launcher_wrote_is_read_into_staging(tmp_path):
    files = {"dist/sub/a.bin": os.urandom(1500), "dist/b.txt": b"b", "c": b""}
    declared = ["dist/sub/a.bin", "dist/b.txt", "c"]
    disk = written_disk(tmp_path, files, declared)
    staging = tmp_path / "staging"
    reading = read(disk, declared, staging, stream=Trickle(disk))
    assert reading == outputs.Reading(result="exited 0", outputs=tuple(declared), refusals=())
    assert staged(staging) == ["c", "dist", "dist/b.txt", "dist/sub", "dist/sub/a.bin"]
    assert all((staging / name).read_bytes() == data for name, data in files.items())
    assert stat.S_IMODE((staging / "dist").stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE((staging / "c").stat().st_mode) & 0o077 == 0


def test_a_missing_output_fails_the_run_and_keeps_the_result(tmp_path):
    disk = archive((RESULT_MEMBER, b"killed 9\n"), ("b", b"b"))
    staging = tmp_path / "staging"
    reading = read(disk, ["a", "b", "c"], staging)
    assert reading.result == "killed 9" and reading.outputs == ()
    assert [(refusal.rule, refusal.detail) for refusal in reading.refusals] == [
        ("output-missing", "declared output 'a' is not on the output disk"),
        ("output-missing", "declared output 'c' is not on the output disk")]
    assert staged(staging) == []


def flipped(data: bytearray, offset: int, value: int = 1) -> bytearray:
    data[offset] ^= value
    return data


def with_header(data: bytearray, offset: int, block: bytes) -> bytearray:
    data[offset:offset + BLOCK] = block
    return data


def raw_header(name: bytes, size_field: bytes) -> bytes:
    block = bytearray(header("placeholder", 0))
    block[0:100] = name.ljust(100, b"\0")
    block[124:136] = size_field
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def tarfile_archive() -> bytearray:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archived:
        info = tarfile.TarInfo(RESULT_MEMBER)
        info.size = 9
        archived.addfile(info, io.BytesIO(b"exited 0\n"))
    data = bytearray(buffer.getvalue())
    return data + bytes(DISK - len(data))


RESULT = (RESULT_MEMBER, b"exited 0\n")
OUTPUT_HEADER = 2 * BLOCK
REFUSED = [
    ("short", lambda: archive(RESULT)[:-1], "disk-length"),
    ("short-in-a-header", lambda: archive(RESULT, ("a", b"a"))[:OUTPUT_HEADER + 100], "disk-length"),
    ("long", lambda: archive(RESULT) + b"\0", "disk-length"),
    ("tarfile", tarfile_archive, "header"),
    ("checksum", lambda: flipped(archive(RESULT), 150), "header"),
    ("mode", lambda: flipped(archive(RESULT), 103, 7), "header"),
    ("mtime", lambda: flipped(archive(RESULT), 140), "header"),
    ("typeflag", lambda: flipped(archive(RESULT), 156, 0x02), "header"),
    ("magic", lambda: flipped(archive(RESULT), 258), "header"),
    ("name-tail", lambda: with_header(archive(RESULT, ("a", b"a")), OUTPUT_HEADER, raw_header(b"a\0x", b"%011o\0" % 1)),
     "header"),
    ("size-not-octal", lambda: with_header(archive(RESULT), 0, raw_header(RESULT_MEMBER.encode(), b"0000000001a\0")),
     "header"),
    ("size-unterminated", lambda: with_header(archive(RESULT), 0, raw_header(RESULT_MEMBER.encode(), b"000000000011")),
     "header"),
    ("name-not-utf8", lambda: with_header(archive(RESULT), 0, raw_header(b"\xff", b"%011o\0" % 9)), "header"),
    ("name-empty", lambda: with_header(archive(RESULT), 0, raw_header(b"", b"%011o\0" % 9)), "header"),
    ("first-not-result", lambda: archive(("a", b"a"), RESULT), "member"),
    ("undeclared", lambda: archive(RESULT, ("SECRET-VALUE", b"x")), "member"),
    ("out-of-order", lambda: archive(RESULT, ("b", b"b"), ("a", b"a")), "member"),
    ("twice", lambda: archive(RESULT, ("a", b"a"), ("a", b"a")), "member"),
    ("result-twice", lambda: archive(RESULT, RESULT), "member"),
    ("result-padding", lambda: flipped(archive(RESULT), BLOCK + 9), "padding"),
    ("output-padding", lambda: flipped(archive(RESULT, ("a", b"a")), OUTPUT_HEADER + BLOCK + 1), "padding"),
    ("past-maximum", lambda: with_header(archive(RESULT), OUTPUT_HEADER, header("a", DISK)), "length"),
    ("second-zero-block", lambda: flipped(archive(RESULT), 4 * BLOCK - 1), "end"),
    ("tail", lambda: flipped(archive(RESULT), 4 * BLOCK), "tail"),
    ("last-byte", lambda: flipped(archive(RESULT), DISK - 1), "tail"),
    ("no-result", lambda: bytearray(DISK), "build-result"),
    ("result-too-long", lambda: archive((RESULT_MEMBER, b"exited 0\n".ljust(17, b" "))), "build-result"),
    ("result-not-a-line", lambda: archive((RESULT_MEMBER, b"exited 256\n")), "build-result"),
]


@pytest.mark.parametrize("make,rule", [(make, rule) for _, make, rule in REFUSED], ids=[case[0] for case in REFUSED])
def test_anything_but_the_launchers_archive_is_refused(tmp_path, make, rule):
    staging = tmp_path / "staging"
    reading = read(bytes(make()), ["a", "b"], staging)
    assert rules_of(reading) == [rule]
    assert reading.result is None and reading.outputs == () and staged(staging) == []
    assert all("SECRET-VALUE" not in refusal.detail for refusal in reading.refusals)


def test_a_maximum_inside_the_disk_bounds_the_archive(tmp_path):
    disk = archive(RESULT, ("a", b"x" * 600))
    end = output_disk.archive_length([9, 600])
    assert read(disk, ["a"], tmp_path / "fits", maximum=end).refusals == ()
    assert rules_of(read(disk, ["a"], tmp_path / "over", maximum=end - 1)) == ["length"]


def test_a_refusal_removes_what_the_read_staged(tmp_path):
    disk = flipped(archive(RESULT, ("d/e/a", b"a" * 700), ("d/b", b"b"), ("c", b"c")), 6 * BLOCK + 1)
    staging = tmp_path / "staging"
    reading = read(bytes(disk), ["d/e/a", "d/b", "c"], staging)
    assert rules_of(reading) == ["padding"] and staged(staging) == []


def test_an_error_while_staging_removes_what_the_read_staged(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "b").write_bytes(b"already here")
    with pytest.raises(FileExistsError):
        read(bytes(archive(RESULT, ("d/a", b"a"), ("b", b"b"))), ["d/a", "b"], staging)
    assert staged(staging) == ["b"] and (staging / "b").read_bytes() == b"already here"


def test_random_corruption_never_crashes_and_breaks_every_check_outside_data(tmp_path):
    members = [RESULT, ("dist/a.bin", bytes(range(256)) * 3), ("b", b"bb")]
    clean = archive(*members, size=8192)
    data_regions, offset = [], 0
    for _, content in members:
        data_regions.append(range(offset + BLOCK, offset + BLOCK + len(content)))
        offset += BLOCK + output_disk.padded(len(content))
    generator = random.Random(202)
    for trial in range(400):
        corrupt = bytearray(clean)
        position = generator.randrange(len(corrupt))
        corrupt[position] ^= 1 << generator.randrange(8)
        staging = tmp_path / f"staging-{trial}"
        reading = read(bytes(corrupt), ["dist/a.bin", "b"], staging, maximum=8192, size=8192)
        if not any(position in region for region in data_regions):
            assert reading.refusals and reading.outputs == (), position
        if reading.refusals:
            assert staged(staging) == []


# The declaration's limits come from the format


def example(**changes: object) -> dict:
    data = declaration.parse_json(EXAMPLE_DECLARATION.read_text(encoding="utf-8"))
    data.update(changes)
    return data


@pytest.mark.parametrize("changes", [
    {"outputs": ["x" * 100]}, {"outputs": ["d/" + "é" * 49]}, {"outputs": ["dist", "dist2/a", "distx"]},
    {"maxOutputBytes": 2048},
])
def test_the_format_limits_admit_their_bounds(changes):
    assert declaration.check_declaration(example(**changes), "0" * 64)[1] == []


@pytest.mark.parametrize("changes,rule", [
    ({"outputs": ["x" * 101]}, "output-path"), ({"outputs": ["d/" + "é" * 50]}, "output-path"),
    ({"outputs": ["dist/a/b", "dist"]}, "output-path"), ({"maxOutputBytes": 2047}, "output-size"),
])
def test_the_format_limits_refuse_past_their_bounds(changes, rule):
    assert [refusal.rule for refusal in declaration.check_declaration(example(**changes), "0" * 64)[1]] == [rule]
