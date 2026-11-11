from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPOSITORY / "vm" / "windows-labview" / "vnc-bootstrap.py"


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("vnc_bootstrap_test", BOOTSTRAP)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load bootstrap module from {BOOTSTRAP}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self):
        self.current = 0.0

    def monotonic(self):
        return self.current

    def sleep(self, seconds):
        self.current += seconds


@pytest.fixture
def bootstrap(monkeypatch):
    module = load_bootstrap()
    monkeypatch.setattr(module, "PROGRESS_HEARTBEAT_SECONDS", 20.0)
    monkeypatch.setattr(module, "time", FakeClock())
    return module


def test_marker_wait_emits_progress_without_echoing_log_content(bootstrap, tmp_path, capsys):
    secret_canary = "VM_PASSWORD_CANARY"
    log_path = tmp_path / "VBox.log"
    log_path.write_text(f"setup continues {secret_canary}", encoding="utf-8")

    result = bootstrap.wait_for_marker_count(
        log_path, "VBoxTray.exe", 1, timeout=55.0, poll_interval=10.0
    )

    output = capsys.readouterr().err
    assert result is False
    assert "still waiting for marker 'VBoxTray.exe' (0/1" in output
    assert "20s elapsed" in output
    assert "40s elapsed" in output
    assert secret_canary not in output


def test_winrm_wait_emits_progress_without_echoing_transport_error(bootstrap, monkeypatch, capsys):
    secret_canary = "WINRM_PASSWORD_CANARY"

    def unavailable(_url, timeout):
        raise bootstrap.urllib.error.URLError(f"listener not ready {secret_canary}")

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", unavailable)
    result = bootstrap.poll_winrm_ready(
        "127.0.0.1", 25993, timeout=55.0, poll_interval=10.0
    )

    output = capsys.readouterr().err
    assert result is False
    assert "still waiting for WinRM at 127.0.0.1:25993" in output
    assert "20s elapsed" in output
    assert "40s elapsed" in output
    assert secret_canary not in output