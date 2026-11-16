"""Harness-only scorer, deliberately separate from model-visible task output."""
from __future__ import annotations

from dataclasses import dataclass
import re

from assisted_benchmark_synthetic_ui import SyntheticUiWorkload


@dataclass(frozen=True)
class OracleResult:
    state_grounded: bool
    action_correct: bool
    task_success: bool


class SyntheticUiOracle:
    """Score actual workload state; never call this from an MCP tool handler."""

    @staticmethod
    def _expected_case(seed: int) -> tuple[int, int, str]:
        case = seed % 3
        if case == 0:
            return 32 + seed % 17, 64, "approve"
        if case == 1:
            return 72 + seed % 19, 61 + seed % 11, "hold"
        reading = 60 + seed % 21
        return reading, reading, "approve"

    def evaluate(self, workload: SyntheticUiWorkload, state_claim: object) -> OracleResult:
        receipt = workload.receipt
        if receipt is None:
            raise ValueError("task has no submitted action")
        return self.evaluate_observation(workload.task_id, workload.visible_view(), receipt.target_id, state_claim)

    def evaluate_observation(self, task_id: str, visible_state: object,
                             target_id: object, state_claim: object) -> OracleResult:
        if not isinstance(task_id, str) or not re.fullmatch(r"synthetic-[a-f0-9]{8}", task_id):
            raise ValueError("synthetic UI task identity is invalid")
        seed = int(task_id.removeprefix("synthetic-"), 16)
        expected_reading, expected_limit, expected_target = self._expected_case(seed)
        observation_matches = (
            isinstance(visible_state, dict)
            and type(visible_state.get("reading")) is int
            and type(visible_state.get("limit")) is int
            and visible_state["reading"] == expected_reading
            and visible_state["limit"] == expected_limit
        )
        claim_matches = (
            isinstance(state_claim, dict)
            and set(state_claim) == {"reading", "limit"}
            and type(state_claim["reading"]) is int
            and type(state_claim["limit"]) is int
            and state_claim["reading"] == expected_reading
            and state_claim["limit"] == expected_limit
        )
        action_correct = target_id == expected_target
        state_grounded = observation_matches and claim_matches
        task_success = (isinstance(visible_state, dict)
                        and visible_state.get("state") == "decision_recorded" and action_correct)
        return OracleResult(
            state_grounded=state_grounded,
            action_correct=action_correct,
            task_success=task_success,
        )