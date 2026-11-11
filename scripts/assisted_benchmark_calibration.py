"""Validate and summarize non-qualifying live-assistance calibration records."""
from __future__ import annotations

import hashlib
import math
import random
import re


MODES = ("on-demand", "periodic", "continuous")
REQUIRED_FIELDS = {
    "trialId", "mode", "workloadVersion", "scenarioSeed", "modelDigest", "modelSettingsSha256",
    "hostProfile", "hostResourceReserve", "guestImageSha256", "guestResourceProfile",
    "captureConfigurationSha256", "evidenceAvailableNs", "firstTokenNs", "finalTokenNs",
    "actionAppliedNs", "stateGrounded", "actionCorrect", "taskSuccess",
}
FIXED_FIELDS = (
    "workloadVersion", "modelDigest", "modelSettingsSha256", "hostProfile", "hostResourceReserve",
    "guestImageSha256", "guestResourceProfile", "captureConfigurationSha256",
)
BOOTSTRAP_RESAMPLES = 500


class CalibrationError(ValueError):
    """Calibration records cannot form a comparable, non-qualifying dataset."""


def _validate_record(record: object) -> dict:
    if not isinstance(record, dict) or set(record) != REQUIRED_FIELDS:
        raise CalibrationError("calibration record fields are invalid")
    if not isinstance(record["trialId"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", record["trialId"]):
        raise CalibrationError("calibration trial identity is invalid")
    if record["mode"] not in MODES:
        raise CalibrationError("calibration mode is invalid")
    if (type(record["scenarioSeed"]) is not int or not 0 <= record["scenarioSeed"] <= 2**32 - 1
            or record["workloadVersion"] != "projectbuddy/synthetic-ui-task@1"):
        raise CalibrationError("calibration workload identity is invalid")
    for key in ("modelDigest", "modelSettingsSha256", "guestImageSha256", "captureConfigurationSha256"):
        if not isinstance(record[key], str) or not re.fullmatch(r"[0-9a-f]{64}", record[key]):
            raise CalibrationError(f"calibration {key} must be a lowercase SHA-256 digest")
    for key in ("hostProfile", "hostResourceReserve", "guestResourceProfile"):
        if not isinstance(record[key], str) or not record[key].strip():
            raise CalibrationError(f"calibration {key} must be non-empty")
    times = ("evidenceAvailableNs", "firstTokenNs", "finalTokenNs", "actionAppliedNs")
    if any(type(record[key]) is not int or record[key] < 0 for key in times):
        raise CalibrationError("calibration timestamps must be non-negative monotonic integers")
    if not (record["evidenceAvailableNs"] <= record["firstTokenNs"] <= record["finalTokenNs"]
            and record["evidenceAvailableNs"] <= record["actionAppliedNs"]):
        raise CalibrationError("calibration timestamps are out of order")
    for key in ("stateGrounded", "actionCorrect", "taskSuccess"):
        if type(record[key]) is not bool:
            raise CalibrationError(f"calibration {key} must be boolean")
    return dict(record)


def _nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _confidence_interval(values: list[int], percentile: float, key: str) -> list[int] | None:
    if len(values) < 2:
        return None
    seed = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)
    estimates = [
        _nearest_rank([values[generator.randrange(len(values))] for _ in values], percentile)
        for _ in range(BOOTSTRAP_RESAMPLES)
    ]
    return [_nearest_rank(estimates, 0.025), _nearest_rank(estimates, 0.975)]


def _latency_summary(values: list[int], label: str, mode: str) -> dict:
    return {
        "p50Ns": _nearest_rank(values, 0.50),
        "p50Bootstrap95CiNs": _confidence_interval(values, 0.50, f"{mode}:{label}:p50"),
        "p95Ns": _nearest_rank(values, 0.95),
        "p95Bootstrap95CiNs": _confidence_interval(values, 0.95, f"{mode}:{label}:p95"),
    }


def summarize_calibration(records: object) -> dict:
    if not isinstance(records, list) or not records:
        raise CalibrationError("calibration dataset must be a non-empty array")
    normalized = [_validate_record(record) for record in records]
    first = normalized[0]
    fixed_values = {key: first[key] for key in FIXED_FIELDS}
    if any(any(record[key] != fixed_values[key] for key in FIXED_FIELDS) for record in normalized[1:]):
        raise CalibrationError("calibration records do not share one fixed environment and model")

    trials: set[tuple[str, str]] = set()
    seeds_by_mode: dict[str, set[int]] = {mode: set() for mode in MODES}
    grouped: dict[str, list[dict]] = {mode: [] for mode in MODES}
    for record in normalized:
        key = (record["mode"], record["trialId"])
        if key in trials:
            raise CalibrationError("calibration trial is duplicated within a mode")
        trials.add(key)
        seeds_by_mode[record["mode"]].add(record["scenarioSeed"])
        grouped[record["mode"]].append(record)
    if any(not grouped[mode] for mode in MODES):
        raise CalibrationError("calibration dataset must include every interaction mode")
    if any(seeds_by_mode[mode] != seeds_by_mode[MODES[0]] for mode in MODES[1:]):
        raise CalibrationError("interaction modes must use the same scenario seed set")

    summary = {}
    for mode in MODES:
        entries = grouped[mode]
        evidence = [item["evidenceAvailableNs"] for item in entries]
        first_token = [item["firstTokenNs"] - item["evidenceAvailableNs"] for item in entries]
        final_token = [item["finalTokenNs"] - item["evidenceAvailableNs"] for item in entries]
        action = [item["actionAppliedNs"] - item["evidenceAvailableNs"] for item in entries]
        count = len(entries)
        summary[mode] = {
            "sampleCount": count,
            "scenarioSeeds": sorted(seeds_by_mode[mode]),
            "firstTokenLatency": _latency_summary(first_token, "first-token", mode),
            "finalTokenLatency": _latency_summary(final_token, "final-token", mode),
            "actionLatency": _latency_summary(action, "action", mode),
            "stateGroundingRate": sum(item["stateGrounded"] for item in entries) / count,
            "actionCorrectRate": sum(item["actionCorrect"] for item in entries) / count,
            "taskSuccessRate": sum(item["taskSuccess"] for item in entries) / count,
        }
    return {
        "schema": "projectbuddy/synthetic-ui-calibration-summary@1",
        "qualification": "not-qualified",
        "fixedConfiguration": fixed_values,
        "modes": summary,
    }