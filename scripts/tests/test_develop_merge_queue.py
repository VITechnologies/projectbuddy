"""Upstream develop is the only branch subject to the queue; nobody bypasses it."""
import json
from pathlib import Path


def test_develop_queue_scope_and_enforcement():
    root = Path(__file__).resolve().parents[2]
    ruleset = json.loads((root / ".github/rulesets/develop-merge-queue.json").read_text(encoding="utf-8"))

    assert ruleset["target"] == "branch"
    assert ruleset["conditions"] == {"ref_name": {"include": ["refs/heads/develop"], "exclude": []}}
    assert ruleset["bypass_actors"] == []
    assert ruleset["enforcement"] == "active"
    assert [rule["type"] for rule in ruleset["rules"]] == ["merge_queue"]
