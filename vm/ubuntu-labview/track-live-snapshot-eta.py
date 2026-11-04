#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import uuid

ACTIVATED_SNAPSHOT_NAME = "labview2026-q3-pro-activated-ready"


def parse_vm_info(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        match = re.fullmatch(r'([^=]+)=(?:"(.*)"|(.*))', line)
        if match:
            values[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return values


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "warming-up"
    if seconds <= 0:
        return "complete"
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def latest_save_path(vm_dir: pathlib.Path) -> pathlib.Path | None:
    snapshots = vm_dir / "Snapshots"
    if not snapshots.exists():
        return None
    saves = sorted(snapshots.glob("*.sav"), key=lambda path: path.stat().st_mtime, reverse=True)
    return saves[0] if saves else None


def operation_elapsed_seconds(vm_info: dict[str, str], now: float) -> float | None:
    changed = vm_info.get("VMStateChangeTime")
    if not changed:
        return None
    try:
        started = datetime.datetime.fromisoformat(changed.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, now - started.timestamp())


def load_history(path: pathlib.Path) -> dict[str, object]:
    if not path.exists():
        return {"samples": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"samples": []}
    if isinstance(raw, dict) and "samples" in raw:
        return raw
    if isinstance(raw, dict):
        samples = [{
            "save_file": raw.get("save_file"),
            "size_bytes": raw.get("size_bytes", 0),
            "sample_time": raw.get("sample_time", 0),
        }]
        return {"samples": samples}
    return {"samples": []}


def write_history(path: pathlib.Path, value: dict[str, object]) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def estimate_sample(
    vm_name: str,
    vm_info: dict[str, str],
    target_size: int | None,
    expected_snapshot_uuid: str | None,
    state_file: pathlib.Path,
) -> dict[str, object]:
    vm_dir = pathlib.Path(vm_info.get("CfgFile", "")).resolve().parent if vm_info.get("CfgFile") else None
    if vm_dir is None or not vm_dir.exists():
        raise RuntimeError(f"VM configuration path is missing for {vm_name}")

    save_path = latest_save_path(vm_dir)
    if save_path is None:
        raise RuntimeError(f"No .sav snapshot state found under {vm_dir / 'Snapshots'}")

    current_size = save_path.stat().st_size
    memory_bytes = int(vm_info.get("memory", "0") or "0") * 1024 * 1024
    current_snapshot_name = vm_info.get("CurrentSnapshotName", "unknown")
    current_snapshot_uuid = vm_info.get("CurrentSnapshotUUID", "unknown")
    current_state = vm_info.get("VMState", "unknown")
    snapshot_complete = (
        expected_snapshot_uuid is not None
        and current_snapshot_name == ACTIVATED_SNAPSHOT_NAME
        and current_snapshot_uuid.lower() == expected_snapshot_uuid
    )
    history = load_history(state_file)
    samples = history.get("samples", [])
    if not isinstance(samples, list):
        samples = []

    sample_record = {
        "save_file": str(save_path),
        "size_bytes": current_size,
        "sample_time": time.time(),
    }
    if not samples or samples[-1].get("save_file") != str(save_path):
        samples = [sample_record]
    else:
        samples.append(sample_record)
    samples = samples[-10:]

    rate = 0.0
    if len(samples) >= 2:
        total_delta_bytes = 0.0
        total_delta_seconds = 0.0
        for previous, current in zip(samples, samples[1:]):
            if previous.get("save_file") != current.get("save_file"):
                continue
            previous_size = float(previous.get("size_bytes", 0) or 0)
            current_size_value = float(current.get("size_bytes", 0) or 0)
            previous_time = float(previous.get("sample_time", 0) or 0)
            current_time = float(current.get("sample_time", 0) or 0)
            if previous_size <= 0 or current_time <= previous_time:
                continue
            delta_bytes = max(0.0, current_size_value - previous_size)
            delta_seconds = max(1.0, current_time - previous_time)
            total_delta_bytes += delta_bytes
            total_delta_seconds += delta_seconds
        if total_delta_seconds > 0:
            rate = total_delta_bytes / total_delta_seconds

    remaining = max(0, target_size - current_size) if target_size is not None else None
    eta_seconds = None if remaining is None or rate <= 0 or remaining <= 0 else remaining / rate
    elapsed_seconds = operation_elapsed_seconds(vm_info, float(sample_record["sample_time"]))
    save_to_memory_ratio = current_size / memory_bytes if memory_bytes > 0 else None
    if (
        current_state == "livesnapshotting"
        and elapsed_seconds is not None
        and elapsed_seconds >= 3600
        and save_to_memory_ratio is not None
        and save_to_memory_ratio >= 2
    ):
        health_signal = "suspected-non-converging-live-precopy"
    elif current_state == "onlinesnapshotting":
        health_signal = "guest-suspended-final-copy"
    else:
        health_signal = "no-oversize-signal"
    write_history(state_file, {"samples": samples})

    return {
        "vm_name": vm_name,
        "vm_state": current_state,
        "current_snapshot": current_snapshot_name,
        "current_snapshot_uuid": current_snapshot_uuid,
        "expected_snapshot_uuid": expected_snapshot_uuid,
        "snapshot_complete": snapshot_complete,
        "save_file": str(save_path),
        "size_bytes": current_size,
        "memory_bytes": memory_bytes,
        "save_to_memory_ratio": save_to_memory_ratio,
        "operation_elapsed_seconds": elapsed_seconds,
        "health_signal": health_signal,
        "target_size_bytes": target_size,
        "remaining_bytes": remaining,
        "bytes_per_second": rate,
        "eta_seconds": eta_seconds,
    }


def run_vboxmanage(vboxmanage: str, vm_name: str) -> dict[str, str]:
    result = subprocess.run(
        [vboxmanage, "showvminfo", vm_name, "--machinereadable"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "VBoxManage showvminfo failed")
    return parse_vm_info(result.stdout)


def render_line(sample: dict[str, object]) -> str:
    vm_state = sample["vm_state"]
    current_snapshot = sample["current_snapshot"]
    current_snapshot_uuid = sample["current_snapshot_uuid"]
    expected_snapshot_uuid = sample["expected_snapshot_uuid"]
    save_file = sample["save_file"]
    size_bytes = int(sample["size_bytes"])
    target_size_value = sample["target_size_bytes"]
    target_size_bytes = int(target_size_value) if target_size_value is not None else None
    memory_bytes = int(sample["memory_bytes"])
    save_to_memory_ratio = sample["save_to_memory_ratio"]
    operation_elapsed = sample["operation_elapsed_seconds"]
    rate = float(sample["bytes_per_second"])
    remaining_value = sample["remaining_bytes"]
    remaining_bytes = int(remaining_value) if remaining_value is not None else None
    eta_seconds = sample["eta_seconds"]
    if sample["snapshot_complete"]:
        eta_text = "complete"
        finalization_gate = "ready"
    elif current_snapshot == ACTIVATED_SNAPSHOT_NAME and expected_snapshot_uuid is None:
        eta_text = "current-name-awaiting-uuid-verification"
        finalization_gate = "blocked-missing-expected-uuid"
    elif current_snapshot == ACTIVATED_SNAPSHOT_NAME:
        eta_text = "current-identity-mismatch"
        finalization_gate = "blocked-identity-mismatch"
    elif target_size_bytes is None:
        eta_text = "unavailable-no-authoritative-target"
        finalization_gate = "blocked-snapshot-not-current"
    elif remaining_bytes is not None and remaining_bytes <= 0:
        eta_text = "target-reached-awaiting-current"
        finalization_gate = "blocked-snapshot-not-current"
    elif eta_seconds is None:
        eta_text = "warming-up"
        finalization_gate = "blocked-snapshot-not-current"
    else:
        eta_text = format_duration(float(eta_seconds))
        finalization_gate = "blocked-snapshot-not-current"
    ratio_text = f"{float(save_to_memory_ratio):.2f}" if save_to_memory_ratio is not None else "unknown"
    elapsed_text = format_elapsed(float(operation_elapsed))
    target_text = str(target_size_bytes) if target_size_bytes is not None else "unknown"
    remaining_text = str(remaining_bytes) if remaining_bytes is not None else "unknown"
    return (
        "live_snapshot_eta: "
        "eta_authoritative=false "
        "gate_authority=VirtualBox-current-snapshot-name-and-uuid "
        f"finalization_gate={finalization_gate} "
        f"vm={sample['vm_name']} "
        f"VMState={vm_state} "
        f"current_snapshot={current_snapshot} "
        f"current_snapshot_uuid={current_snapshot_uuid} "
        f"save_file={save_file} "
        f"size_bytes={size_bytes} "
        f"memory_bytes={memory_bytes} "
        f"save_to_memory_ratio={ratio_text} "
        f"operation_elapsed={elapsed_text} "
        f"health_signal={sample['health_signal']} "
        f"target_size_bytes={target_text} "
        f"remaining_bytes={remaining_text} "
        f"bytes_per_sec={rate:.0f} "
        f"eta={eta_text}"
    )


def iter_samples(
    vboxmanage: str,
    vm_name: str,
    target_size: int | None,
    expected_snapshot_uuid: str | None,
    interval: float,
    loop: bool,
    iterations: int | None,
    state_file: pathlib.Path,
):
    count = 0
    while True:
        vm_info = run_vboxmanage(vboxmanage, vm_name)
        sample = estimate_sample(vm_name, vm_info, target_size, expected_snapshot_uuid, state_file)
        yield sample

        if not loop:
            break
        count += 1
        if iterations is not None and count >= iterations:
            break
        time.sleep(interval)
        if vm_info.get("VMState") != "livesnapshotting":
            break


def main() -> int:
    parser = argparse.ArgumentParser(description="Track the ETA of a VirtualBox live snapshot")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vboxmanage", default="VBoxManage")
    parser.add_argument("--target-size-bytes", type=int)
    parser.add_argument("--expected-snapshot-uuid")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--state-file", type=pathlib.Path, default=pathlib.Path.home() / ".local" / "state" / "sandbox-sbom-reporting" / "live-snapshot-eta.json")
    args = parser.parse_args()

    if args.target_size_bytes is not None and args.target_size_bytes <= 0:
        parser.error("--target-size-bytes must be positive")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if not args.state_file.is_absolute():
        parser.error("--state-file must be absolute")
    expected_snapshot_uuid = None
    if args.expected_snapshot_uuid:
        try:
            expected_snapshot_uuid = str(uuid.UUID(args.expected_snapshot_uuid))
        except ValueError:
            parser.error("--expected-snapshot-uuid must be a UUID")

    try:
        vm_info = run_vboxmanage(args.vboxmanage, args.vm_name)
        target_size = args.target_size_bytes
        loop_iterations = args.iterations if args.loop else 1
        if args.loop and loop_iterations <= 0:
            loop_iterations = None

        for sample in iter_samples(
            args.vboxmanage,
            args.vm_name,
            target_size,
            expected_snapshot_uuid,
            args.interval,
            args.loop,
            loop_iterations,
            args.state_file,
        ):
            print(render_line(sample))
            if not args.loop:
                break
            if sample["snapshot_complete"]:
                print("live_snapshot_eta: complete: target snapshot is now current")
                break
            if sample["vm_state"] != "livesnapshotting":
                print(f"live_snapshot_eta: stop: VM is no longer in livesnapshotting state ({sample['vm_state']})")
                break
    except (OSError, RuntimeError, ValueError) as error:
        print(f"live_snapshot_eta: FAIL: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
