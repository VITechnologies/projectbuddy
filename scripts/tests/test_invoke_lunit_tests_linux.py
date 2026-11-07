"""Real, assertion-based unit tests for invoke_lunit_tests_linux.py's
validate_junit_report function -- the pure, offline-testable JUnit
report/waiver-policy logic ADR-0003's round-2 Addendum and REQ-036
require (named waivers only; every other failure/error blocks the gate).
Mirrors scripts/tests/Invoke-LUnitTests.Tests.ps1's coverage for the
Windows gate's equivalent function.

Run with: pytest --cov=invoke_lunit_tests_linux --cov-report=xml scripts/tests/
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent.parent / "docker" / "lunit-gate" / "invoke_lunit_tests_linux.py"
_spec = importlib.util.spec_from_file_location("invoke_lunit_tests_linux", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
invoke_lunit_tests_linux = importlib.util.module_from_spec(_spec)
sys.modules["invoke_lunit_tests_linux"] = invoke_lunit_tests_linux
_spec.loader.exec_module(invoke_lunit_tests_linux)

ALL_PASS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="3" failures="0" errors="0">
  <testcase classname="History Tests" name="Test Undo.vi" />
  <testcase classname="Editor Position" name="Test Save Position.vi" />
  <testcase classname="INI Settings" name="Test Read Setting.vi" />
</testsuite>
"""

ALL_NAMED_WAIVERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="4" failures="3" errors="0">
  <testcase classname="Library Icon Data" name="Test Write Read Library Icon Data.vi">
    <failure message="NI.Lib.VIIconTemplate expected versus NI_IconEditor saved">tag mismatch</failure>
  </testcase>
  <testcase classname="Editor Position" name="Test Window Position.vi">
    <failure message="1-pixel offset, Linux window manager">pixel offset</failure>
  </testcase>
  <testcase classname="INI Settings" name="Test INI Settings.vi">
    <failure message="Small Fonts versus LabVIEW Application">font name mismatch</failure>
  </testcase>
  <testcase classname="History Tests" name="Test Undo.vi" />
</testsuite>
"""

BLOCKING_FAILURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="2" failures="1" errors="0">
  <testcase classname="History Tests" name="Test Undo.vi">
    <failure message="unexpected assertion failure">boom</failure>
  </testcase>
  <testcase classname="Editor Position" name="Test Save Position.vi" />
</testsuite>
"""

BLOCKING_ERROR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="1" failures="0" errors="1">
  <testcase classname="INI Settings" name="Test Read Setting.vi">
    <error message="unhandled exception">crashed</error>
  </testcase>
</testsuite>
"""

EMPTY_SUITE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="0" failures="0" errors="0">
</testsuite>
"""


def _write_fixture(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "lunit.xml"
    path.write_text(content, encoding="utf-8")
    return path


def test_passes_when_every_test_case_is_clean(tmp_path):
    path = _write_fixture(tmp_path, ALL_PASS_XML)
    result = invoke_lunit_tests_linux.validate_junit_report(path, "abc123")
    assert result["outcome"] == "PASS"
    assert result["totalTests"] == 3
    assert result["waivedTests"] == []


def test_passes_and_records_all_three_named_linux_waivers(tmp_path):
    path = _write_fixture(tmp_path, ALL_NAMED_WAIVERS_XML)
    result = invoke_lunit_tests_linux.validate_junit_report(path, "abc123")
    assert result["outcome"] == "PASS"
    assert set(result["waivedTests"]) == {
        "Test Write Read Library Icon Data.vi",
        "Test Window Position.vi",
        "Test INI Settings.vi",
    }


def test_raises_blocking_failure_on_any_other_test(tmp_path):
    path = _write_fixture(tmp_path, BLOCKING_FAILURE_XML)
    with pytest.raises(RuntimeError, match="Test Undo.vi"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123")


def test_raises_blocking_error_not_just_failure(tmp_path):
    path = _write_fixture(tmp_path, BLOCKING_ERROR_XML)
    with pytest.raises(RuntimeError, match="Test Read Setting.vi"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123")


def test_raises_when_report_has_zero_test_cases(tmp_path):
    path = _write_fixture(tmp_path, EMPTY_SUITE_XML)
    with pytest.raises(RuntimeError, match="zero test cases"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123")


def test_raises_when_no_report_file_exists(tmp_path):
    path = tmp_path / "does-not-exist.xml"
    with pytest.raises(RuntimeError, match="No JUnit report"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123")


def test_allowed_waiver_set_has_exactly_three_named_tests():
    assert invoke_lunit_tests_linux.ALLOWED_WAIVER_TEST_NAMES == {
        "Test Write Read Library Icon Data.vi",
        "Test Window Position.vi",
        "Test INI Settings.vi",
    }
