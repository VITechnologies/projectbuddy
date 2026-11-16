"""Windows VirtualBox builder must not expose unattended credentials."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
BUILDER = REPOSITORY / "vm" / "windows-labview" / "build-virtualbox.sh"


def test_unattended_output_redacts_password_fields_and_preserves_diagnostics():
    source = BUILDER.read_text(encoding="utf-8")
    function = source.split("run_redacted() {", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    match = re.search(r"sed -E '([^']+)'", function)
    assert match is not None
    assert '>"$raw_output" 2>&1' in function
    assert 'cat "$safe_output" >&2' in function

    result = subprocess.run(
        ["sed", "-E", match.group(1)],
        input=(
            "Password: CANARY_ONE\n"
            "user-password = CANARY_TWO\n"
            "admin-password: CANARY_THREE\n"
            "progress: configuring guest\n"
        ),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "CANARY_" not in result.stdout
    assert result.stdout.count("<redacted>") == 3
    assert "progress: configuring guest" in result.stdout