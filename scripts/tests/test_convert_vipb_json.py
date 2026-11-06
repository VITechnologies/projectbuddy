"""Real, assertion-based unit tests for Convert-VipbJson.py's pure XML<->JSON
and dot-path-patch logic -- the genuinely unit-testable parts of this
engine's scripts (as opposed to the VM-provisioning/pipeline scripts,
which need a live VM/VIPM toolchain to exercise meaningfully and are
validated via real CI dispatch instead, see docs/requirements/srs.md
REQ-008/REQ-009).

Run with: pytest --cov=Convert-VipbJson --cov-report=xml scripts/tests/
"""
from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "Convert-VipbJson.py"
_spec = importlib.util.spec_from_file_location("convert_vipb_json", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
convert_vipb_json = importlib.util.module_from_spec(_spec)
sys.modules["convert_vipb_json"] = convert_vipb_json
_spec.loader.exec_module(convert_vipb_json)


# --- _element_to_json -------------------------------------------------

def test_element_to_json_simple_text_leaf():
    root = ET.fromstring("<Name>MyLibrary</Name>")
    assert convert_vipb_json._element_to_json(root) == "MyLibrary"


def test_element_to_json_empty_element_is_none():
    root = ET.fromstring("<Empty/>")
    assert convert_vipb_json._element_to_json(root) is None


def test_element_to_json_attributes_become_at_prefixed_keys():
    root = ET.fromstring('<Item Type="String" Name="X"/>')
    result = convert_vipb_json._element_to_json(root)
    assert result == {"@Type": "String", "@Name": "X"}


def test_element_to_json_nested_children_become_keys():
    root = ET.fromstring("<Settings><Folder>src</Folder><Output>builds</Output></Settings>")
    result = convert_vipb_json._element_to_json(root)
    assert result == {"Folder": "src", "Output": "builds"}


def test_element_to_json_repeated_sibling_tags_become_array():
    root = ET.fromstring("<Items><Item>a</Item><Item>b</Item><Item>c</Item></Items>")
    result = convert_vipb_json._element_to_json(root)
    assert result == {"Item": ["a", "b", "c"]}


def test_element_to_json_text_alongside_children_becomes_hash_text():
    root = ET.fromstring("<Node>loose text<Child>value</Child></Node>")
    result = convert_vipb_json._element_to_json(root)
    assert result == {"Child": "value", "#text": "loose text"}


# --- _find_leaf ---------------------------------------------------------

def test_find_leaf_resolves_a_real_dot_path():
    root = ET.fromstring(
        "<Root><General><Source>src</Source></General></Root>"
    )
    leaf = convert_vipb_json._find_leaf(root, "General.Source")
    assert leaf.text == "src"


def test_find_leaf_raises_clear_keyerror_on_missing_segment():
    root = ET.fromstring("<Root><General><Source>src</Source></General></Root>")
    with pytest.raises(KeyError, match=r"no '<Missing>' element"):
        convert_vipb_json._find_leaf(root, "General.Missing")


# --- cmd_inspect / cmd_patch (file I/O, via tmp_path) --------------------

@pytest.fixture
def sample_vipb(tmp_path: Path) -> Path:
    vipb = tmp_path / "sample.vipb"
    vipb.write_text(
        "<VI_Package_Builder_Settings>"
        "<Library_General_Settings>"
        "<Library_Source_Folder>src</Library_Source_Folder>"
        "<Library_Output_Folder>builds</Library_Output_Folder>"
        "</Library_General_Settings>"
        "</VI_Package_Builder_Settings>",
        encoding="utf-8",
    )
    return vipb


def test_cmd_inspect_writes_expected_json(sample_vipb: Path, tmp_path: Path):
    out_json = tmp_path / "out.json"
    exit_code = convert_vipb_json.cmd_inspect(sample_vipb, out_json)
    assert exit_code == 0
    data = out_json.read_text(encoding="utf-8")
    assert '"Library_Source_Folder": "src"' in data
    assert '"Library_Output_Folder": "builds"' in data


def test_cmd_inspect_prints_to_stdout_when_no_json_path(sample_vipb: Path, capsys: pytest.CaptureFixture[str]):
    exit_code = convert_vipb_json.cmd_inspect(sample_vipb, None)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert '"Library_Source_Folder": "src"' in captured.out


def test_cmd_patch_applies_a_real_field_change(sample_vipb: Path, tmp_path: Path):
    patch_file = tmp_path / "patch.yaml"
    patch_file.write_text(
        "Library_General_Settings.Library_Source_Folder: new-src\n",
        encoding="utf-8",
    )
    out_vipb = tmp_path / "out.vipb"
    exit_code = convert_vipb_json.cmd_patch(sample_vipb, patch_file, out_vipb)
    assert exit_code == 0
    patched = ET.parse(out_vipb).getroot()
    leaf = convert_vipb_json._find_leaf(patched, "Library_General_Settings.Library_Source_Folder")
    assert leaf.text == "new-src"
    # Untouched field stays exactly as it was.
    unchanged = convert_vipb_json._find_leaf(patched, "Library_General_Settings.Library_Output_Folder")
    assert unchanged.text == "builds"


def test_cmd_patch_rejects_a_non_mapping_patch_file(sample_vipb: Path, tmp_path: Path):
    patch_file = tmp_path / "patch.yaml"
    patch_file.write_text("- just\n- a\n- list\n", encoding="utf-8")
    out_vipb = tmp_path / "out.vipb"
    exit_code = convert_vipb_json.cmd_patch(sample_vipb, patch_file, out_vipb)
    assert exit_code == 2
    assert not out_vipb.exists()


def test_cmd_patch_propagates_missing_path_error(sample_vipb: Path, tmp_path: Path):
    patch_file = tmp_path / "patch.yaml"
    patch_file.write_text("Does.Not.Exist: value\n", encoding="utf-8")
    out_vipb = tmp_path / "out.vipb"
    with pytest.raises(KeyError):
        convert_vipb_json.cmd_patch(sample_vipb, patch_file, out_vipb)


# --- main() / CLI wiring, via direct in-process calls (so coverage.py
# actually measures them; a subprocess invocation would not) -----------

def _run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["Convert-VipbJson.py", *args])
    return convert_vipb_json.main()


def test_cli_inspect_end_to_end(sample_vipb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out_json = tmp_path / "out.json"
    exit_code = _run_cli(monkeypatch, "inspect", str(sample_vipb), str(out_json))
    assert exit_code == 0
    assert out_json.exists()


def test_cli_patch_end_to_end(sample_vipb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    patch_file = tmp_path / "patch.yaml"
    patch_file.write_text("Library_General_Settings.Library_Source_Folder: cli-patched\n", encoding="utf-8")
    out_vipb = tmp_path / "out.vipb"
    exit_code = _run_cli(monkeypatch, "patch", str(sample_vipb), str(patch_file), str(out_vipb))
    assert exit_code == 0
    patched = ET.parse(out_vipb).getroot()
    leaf = convert_vipb_json._find_leaf(patched, "Library_General_Settings.Library_Source_Folder")
    assert leaf.text == "cli-patched"


def test_cli_missing_vipb_file_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_cli(monkeypatch, "inspect", str(tmp_path / "does-not-exist.vipb"))
    assert exit_code == 2
    assert "not found" in capsys.readouterr().err


def test_cli_missing_patch_file_fails_closed(sample_vipb: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_cli(monkeypatch, "patch", str(sample_vipb), str(tmp_path / "missing.yaml"), str(tmp_path / "out.vipb"))
    assert exit_code == 2
    assert "Patch file not found" in capsys.readouterr().err
