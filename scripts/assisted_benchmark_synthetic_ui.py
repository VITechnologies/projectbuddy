"""Deterministic standalone UI task used by live-capture fixture tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import time


WORKLOAD_VERSION = "projectbuddy/synthetic-ui-task@1"
MAX_SCENARIO_SEED = 2**32 - 1
ACTION_TARGETS = frozenset({"approve", "hold"})


class SyntheticUiError(ValueError):
    """A synthetic UI action or task state is invalid."""


@dataclass(frozen=True)
class ScenarioFacts:
    seed: int
    reading: int
    limit: int
    rule: str


@dataclass(frozen=True)
class ActionReceipt:
    task_id: str
    target_id: str
    event_id: str
    submitted_at_ns: int


def scenario_facts(seed: int) -> ScenarioFacts:
    if type(seed) is not int or not 0 <= seed <= MAX_SCENARIO_SEED:
        raise SyntheticUiError("scenario seed is outside the supported range")
    case = seed % 3
    if case == 0:
        reading, limit = 32 + seed % 17, 64
    elif case == 1:
        reading, limit = 72 + seed % 19, 61 + seed % 11
    else:
        reading = limit = 60 + seed % 21
    return ScenarioFacts(seed, reading, limit, "Approve when reading is at or below the limit; otherwise hold.")


class SyntheticUiWorkload:
    """One single-attempt, deterministic task; public views omit oracle state."""

    def __init__(self, seed: int, clock: Callable[[], int] = time.monotonic_ns):
        self._facts = scenario_facts(seed)
        self._clock = clock
        self._state = "awaiting_decision"
        self._receipt: ActionReceipt | None = None
        self._event_sequence = 0
        self.task_id = f"synthetic-{seed:08x}"

    @property
    def state(self) -> str:
        return self._state

    @property
    def receipt(self) -> ActionReceipt | None:
        return self._receipt

    def visible_view(self) -> dict:
        facts = self._facts
        return {
            "workloadVersion": WORKLOAD_VERSION,
            "taskId": self.task_id,
            "state": self._state,
            "instruction": facts.rule,
            "reading": facts.reading,
            "limit": facts.limit,
            "controls": [
                {"targetId": "approve", "label": "Approve"},
                {"targetId": "hold", "label": "Hold"},
            ],
            "eventId": self._event_id(),
        }

    def submit_action(self, target_id: str) -> dict:
        if self._state != "awaiting_decision":
            raise SyntheticUiError("task accepts exactly one action")
        if not isinstance(target_id, str) or target_id not in ACTION_TARGETS:
            raise SyntheticUiError("UI action target is not available")
        self._event_sequence += 1
        receipt = ActionReceipt(
            task_id=self.task_id,
            target_id=target_id,
            event_id=self._event_id(),
            submitted_at_ns=self._clock(),
        )
        self._receipt = receipt
        self._state = "decision_recorded"
        return {
            "accepted": True,
            "taskId": receipt.task_id,
            "state": self._state,
            "eventId": receipt.event_id,
        }

    def _event_id(self) -> str:
        return f"{self.task_id}-event-{self._event_sequence:04d}"