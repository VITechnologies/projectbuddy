"""Real, assertion-based unit tests for invoke_lunit_tests_linux.py's
validate_junit_report and validate_commit_sha functions -- the pure,
offline-testable JUnit report/waiver-policy logic ADR-0003's round-2
Addendum, ADR-0004, and REQ-036 require (named waivers only; every other
failure/error/skip blocks the gate), plus the completeness/engine-commit
checks #94's independent post-implementation verification review added.
Mirrors scripts/tests/Invoke-LUnitTests.Tests.ps1's coverage for the
Windows gate's equivalent functions.

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

_ENGINE_COMMIT = "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed"

# The pinned suite commit's real 10 test cases across 5 classes
# (ni/labview-icon-editor @ d9eac771ccac0ed92bde81a031fe2f4ebe62c845),
# reused verbatim so these fixtures match EXPECTED_TEST_COUNT and look
# like the real lunit.xml (status= attributes included, per #94's finding
# that the prior fixtures didn't resemble real LUnit output).
_ALL_CASE_NAMES = [
    ("Editor Position", "Test Position INI.vi"),
    ("Editor Position", "Test Window Position.vi"),
    ("History Tests", "Test Add Clears Redo.vi"),
    ("History Tests", "Test Add and Undo All.vi"),
    ("History Tests", "Test Overflow.vi"),
    ("History Tests", "Test Undo Flag.vi"),
    ("INI Settings", "Test INI Settings.vi"),
    ("Library Icon Data", "Test Library Icon Tag.vi"),
    ("Library Icon Data", "Test Write Read Library Icon Data.vi"),
    ("Text-Based VI Icon Tests", "Test Split Text into Words.vi"),
]


def _build_ten_case_xml(failing=(), erroring=(), skipped=(), message="boom"):
    cases = []
    for classname, name in _ALL_CASE_NAMES:
        if name in skipped:
            cases.append(f'  <testcase classname="{classname}" name="{name}" status="Skipped"><skipped/></testcase>')
        elif name in failing:
            cases.append(f'  <testcase classname="{classname}" name="{name}" status="Failed"><failure message="{message}">detail</failure></testcase>')
        elif name in erroring:
            cases.append(f'  <testcase classname="{classname}" name="{name}" status="Failed"><error message="{message}">detail</error></testcase>')
        else:
            cases.append(f'  <testcase classname="{classname}" name="{name}" status="Passed"/>')
    body = "\n".join(cases)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="Icon Editor LUnit" tests="10" failures="{len(failing)}" errors="{len(erroring)}">\n'
        f"{body}\n</testsuite>\n"
    )


ALL_PASS_XML = _build_ten_case_xml()
ALL_NAMED_WAIVERS_XML = _build_ten_case_xml(
    failing=["Test Write Read Library Icon Data.vi", "Test Window Position.vi", "Test INI Settings.vi"]
)
BLOCKING_FAILURE_XML = _build_ten_case_xml(failing=["Test Add Clears Redo.vi"], message="unexpected assertion failure")
BLOCKING_ERROR_XML = _build_ten_case_xml(erroring=["Test Add and Undo All.vi"], message="unhandled exception")
# Per #94's review: the waiver list must still block an unwaived 4th
# failure even when all three named waivers are also present in the same
# report (a mutant that only checked "any waived" rather than "every
# failure is waived" would pass this).
THREE_WAIVERS_PLUS_FOURTH_FAILURE_XML = _build_ten_case_xml(
    failing=[
        "Test Write Read Library Icon Data.vi",
        "Test Window Position.vi",
        "Test INI Settings.vi",
        "Test Add Clears Redo.vi",
    ],
    message="mixed waived and unwaived",
)
SKIPPED_TEST_XML = _build_ten_case_xml(skipped=["Test Overflow.vi"])
TRUNCATED_SUITE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="1" failures="0" errors="0">
  <testcase classname="History Tests" name="Test Undo Flag.vi" status="Passed"/>
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
    result = invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)
    assert result["outcome"] == "PASS"
    assert result["totalTests"] == 10
    assert result["waivedTests"] == []


def test_records_the_engine_commit_distinctly_from_the_suite_commit(tmp_path):
    path = _write_fixture(tmp_path, ALL_PASS_XML)
    result = invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)
    assert result["engineCommit"] == _ENGINE_COMMIT
    assert result["suiteCommit"] == "abc123def456abc123def456abc123def456abcd"


def test_passes_and_records_all_three_named_linux_waivers(tmp_path):
    path = _write_fixture(tmp_path, ALL_NAMED_WAIVERS_XML)
    result = invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)
    assert result["outcome"] == "PASS"
    assert set(result["waivedTests"]) == {
        "Test Write Read Library Icon Data.vi",
        "Test Window Position.vi",
        "Test INI Settings.vi",
    }


def test_raises_blocking_failure_on_any_other_test(tmp_path):
    path = _write_fixture(tmp_path, BLOCKING_FAILURE_XML)
    with pytest.raises(RuntimeError, match="Test Add Clears Redo.vi"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)


def test_raises_blocking_error_not_just_failure(tmp_path):
    path = _write_fixture(tmp_path, BLOCKING_ERROR_XML)
    with pytest.raises(RuntimeError, match="Test Add and Undo All.vi"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)


def test_raises_on_unwaived_failure_even_when_named_waivers_also_present(tmp_path):
    path = _write_fixture(tmp_path, THREE_WAIVERS_PLUS_FOURTH_FAILURE_XML)
    with pytest.raises(RuntimeError, match="Test Add Clears Redo.vi"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)


def test_raises_when_report_has_zero_test_cases(tmp_path):
    path = _write_fixture(tmp_path, EMPTY_SUITE_XML)
    with pytest.raises(RuntimeError, match="zero test cases"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)


def test_raises_when_report_is_missing_test_cases(tmp_path):
    path = _write_fixture(tmp_path, TRUNCATED_SUITE_XML)
    with pytest.raises(RuntimeError, match="expected exactly 10"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)


def test_raises_when_a_test_case_is_skipped(tmp_path):
    path = _write_fixture(tmp_path, SKIPPED_TEST_XML)
    with pytest.raises(RuntimeError, match="Test Overflow.vi"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)


def test_raises_when_no_report_file_exists(tmp_path):
    path = tmp_path / "does-not-exist.xml"
    with pytest.raises(RuntimeError, match="No JUnit report"):
        invoke_lunit_tests_linux.validate_junit_report(path, "abc123def456abc123def456abc123def456abcd", _ENGINE_COMMIT)


def test_allowed_waiver_set_has_exactly_three_named_tests():
    assert invoke_lunit_tests_linux.ALLOWED_WAIVER_TEST_NAMES == {
        "Test Write Read Library Icon Data.vi",
        "Test Window Position.vi",
        "Test INI Settings.vi",
    }


def test_assert_no_vipm_presence_checks_real_linux_jki_install_paths():
    """Per #94's independent verification review (3-model convergence):
    the original marker list checked only Windows-shaped paths and
    silently passed against a real Linux VIPM install. This locks in the
    real JKI .deb install locations so a future regression is caught."""
    import inspect

    source = inspect.getsource(invoke_lunit_tests_linux.assert_no_vipm_presence)
    assert "/usr/local/jki/vipm" in source
    assert "/usr/local/bin/vipm" in source


class TestValidateCommitSha:
    def test_accepts_a_full_forty_character_lowercase_hex_commit_sha(self):
        invoke_lunit_tests_linux.validate_commit_sha(
            "d9eac771ccac0ed92bde81a031fe2f4ebe62c845", label="--suite-commit"
        )

    def test_rejects_a_mutable_ref_such_as_a_branch_name(self):
        with pytest.raises(RuntimeError, match="--suite-commit"):
            invoke_lunit_tests_linux.validate_commit_sha("main", label="--suite-commit")

    def test_rejects_an_uppercase_or_short_sha(self):
        with pytest.raises(RuntimeError):
            invoke_lunit_tests_linux.validate_commit_sha("D9EAC77", label="--suite-commit")
