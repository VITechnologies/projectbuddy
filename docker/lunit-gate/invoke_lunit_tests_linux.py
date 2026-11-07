#!/usr/bin/env python3
"""
Downloads a pinned ni/labview-icon-editor commit (as a GitHub archive --
no git dependency, matching the Windows gate's own approach), starts
LabVIEW headless, runs LabVIEWCLI's LUnit operation, and validates the
resulting lunit.xml against the named-waiver-only acceptance policy.

Per ADR-0003/REQ-036 (and the round-2 Addendum's waiver-scope correction):
only a small, explicitly named set of test cases may fail/error without
blocking the gate -- confirmed via direct local Docker testing on this
engine's own host, not assumption. Any other test's failure or error
blocks the gate. Waiving a different test requires a new superseding ADR,
not an edit to ALLOWED_WAIVER_TEST_NAMES below.

No externally-provided Xvfb/DISPLAY setup is needed: confirmed via direct
testing that LabVIEW's VI Server becomes ready and LabVIEWCLI's LUnit
operation runs to completion with no DISPLAY set by this script or image
at all (REQ-035's feasibility question, resolved). LabVIEW itself spawns
its own internal `Xvfb :99` child process when none is externally
provided -- confirmed via #94's independent verification review and a
direct process-tree inspection of the pinned image. This script does not
need to, and must not, manage that framebuffer itself.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

#: A full, 40-character lowercase-hex git commit SHA. Per #94's
#: independent verification review: --suite-commit was previously
#: accepted unvalidated, so a mutable ref (e.g. "main") could silently
#: enter what REQ-036 requires to be a pinned-commit execution.
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

LABVIEW_ROOT = Path("/usr/local/natinst/LabVIEW-2026-64")
LABVIEW_EXE = LABVIEW_ROOT / "labview"
CLI_EXE = "/usr/local/bin/LabVIEWCLI"
WORKSPACE = Path("/lunit-suite-under-test")

# Per ADR-0003's round-2 Addendum (Windows) plus this Phase-2 spike's own
# findings (Linux): each waiver here is individually named and reasoned,
# not a blanket allowance. A different/new waiver requires a new
# superseding ADR.
ALLOWED_WAIVER_TEST_NAMES = {
    # Shared with the Windows gate: a known, disclosed upstream
    # NI.Lib.VIIconTemplate/NI_IconEditor tag-mismatch (see
    # ni/labview-icon-editor#575's own description).
    "Test Write Read Library Icon Data.vi",
    # Linux-specific (confirmed via direct local testing, not assumption):
    # 7 of 8 window-bounds assertions fail by exactly a consistent 1-pixel
    # offset in all four coordinates ("Position when Use Size is False"
    # passes) -- a Linux-window-manager-vs-Windows rendering difference in
    # the test's hardcoded expected values, not a functional defect in
    # this engine's deployment. See #95 for the full tracked waiver.
    "Test Window Position.vi",
    # Linux-specific (confirmed via direct local testing): both of this
    # test's 2 assertions expect Windows-specific default font names
    # ("Small Fonts", "Calibri"); Linux LabVIEW's actual default is
    # "LabVIEW Application" -- a platform default-font-naming difference,
    # not a functional defect. See #96 for the full tracked waiver.
    "Test INI Settings.vi",
}


def assert_no_vipm_presence() -> None:
    # Per #94's independent verification review (3-model): the Linux JKI
    # VIPM .deb (vipm_26.3.2-4056_amd64.deb, as installed by this repo's
    # own docker/linux-sbom/Dockerfile and vm/ubuntu-labview/provision-guest.sh)
    # installs to /usr/local/jki/vipm and /usr/local/bin/vipm, not the
    # Windows-shaped paths originally checked here -- confirmed directly
    # against sandbox-sbom-linux:latest (a real VIPM-installed image in
    # this repo), which the original marker list silently passed.
    import shutil

    vipm_markers = [
        Path("/usr/local/jki/vipm"),
        Path("/usr/local/bin/vipm"),
        Path("/etc/jki"),
        # Kept for defense-in-depth even though not JKI's real Linux
        # install path, in case a future base image layers VIPM
        # differently.
        Path("/usr/local/natinst/VIPM"),
        Path("/root/.config/JKI/VIPM"),
    ]
    for marker in vipm_markers:
        if marker.exists():
            raise RuntimeError(
                f"VIPM presence detected at {marker} -- REQ-036 requires "
                "no VIPM installation anywhere in this container."
            )
    if shutil.which("vipm") is not None:
        raise RuntimeError(
            "A 'vipm' executable is on PATH -- REQ-036 requires no VIPM "
            "installation anywhere in this container."
        )
    for name in ("VIPM_SERIAL_NUMBER", "VIPM_FULL_NAME", "VIPM_EMAIL"):
        if os.environ.get(name):
            raise RuntimeError(
                f"{name} is set in this container's environment -- REQ-036 "
                "requires no VIPM-related secret anywhere."
            )


def get_pinned_suite_archive(owner: str, repo: str, commit: str, destination: Path) -> None:
    """No `git` dependency: downloads GitHub's own commit-archive zip (an
    exact-SHA archive is inherently that commit; GitHub's codeload service
    only serves what actually exists at that SHA, so a successful
    download and extraction is itself the provenance check -- no separate
    verification is meaningful or necessary here)."""
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{commit}"
    zip_path = Path(f"/tmp/{repo}-{commit}.zip")
    extract_root = Path(f"/tmp/{repo}-{commit}-extract")
    urllib.request.urlretrieve(url, zip_path)
    if extract_root.exists():
        import shutil

        shutil.rmtree(extract_root)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_root)
    extracted_folders = [p for p in extract_root.iterdir() if p.is_dir()]
    if not extracted_folders:
        raise RuntimeError(f"GitHub archive for {owner}/{repo}@{commit} extracted no top-level folder.")
    if destination.exists():
        import shutil

        shutil.rmtree(destination)
    extracted_folders[0].rename(destination)
    zip_path.unlink(missing_ok=True)
    import shutil

    shutil.rmtree(extract_root, ignore_errors=True)


def start_labview_headless(project: Path, results_dir: Path) -> subprocess.Popen:
    process = subprocess.Popen(
        [str(LABVIEW_EXE), "--headless", str(project)],
        stdout=open(results_dir / "labview-test.log", "w"),
        stderr=open(results_dir / "labview-test.stderr.log", "w"),
    )
    deadline = time.time() + 180
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"LabVIEW exited with code {process.returncode} before the VI Server became ready.")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            try:
                sock.connect(("127.0.0.1", 3363))
                return process
            except OSError:
                pass
        time.sleep(2)
    raise RuntimeError("LabVIEW VI Server did not become ready on port 3363 within 180 seconds.")


# The pinned suite commit's real test-case count (ni/labview-icon-editor
# @ d9eac771ccac0ed92bde81a031fe2f4ebe62c845, 5 test classes). Per #94's
# review: without this check, a report missing or skipping test cases
# (e.g. a truncated run) could still report "PASS" with zero failures
# among whatever cases happened to run -- REQ-036 requires the pinned
# suite's every test case to run, not merely that none of the cases
# present failed. Bumping suite_commit is a deliberate pin update (see
# docs/lunit-gate/procedure.md's "Suite commit pinning"); revisit this
# count at the same time.
EXPECTED_TEST_COUNT = 10


def validate_junit_report(report_path: Path, suite_commit: str, engine_commit: str) -> dict:
    if not report_path.exists():
        raise RuntimeError(f"No JUnit report was produced at {report_path}.")

    tree = ET.parse(report_path)
    cases = tree.getroot().findall(".//testcase")
    if not cases:
        raise RuntimeError("JUnit report contains zero test cases -- the pinned suite did not run.")
    if len(cases) != EXPECTED_TEST_COUNT:
        raise RuntimeError(
            f"JUnit report contains {len(cases)} test case(s); expected exactly "
            f"{EXPECTED_TEST_COUNT} for the pinned suite commit {suite_commit}. "
            "A partial/truncated run must not pass the gate."
        )

    blocking = []
    waived = []
    for case in cases:
        name = case.get("name")
        status = case.get("status")
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None and case.find("skipped") is None and status in (None, "Passed", "Failed"):
            continue
        if problem is None:
            # Not a failure/error element, but also not a recognized
            # "Passed"/"Failed" status (e.g. a <skipped/> element or a
            # status like "Inconclusive") -- REQ-036 requires every test
            # case to actually run, not be skipped/inconclusive, so this
            # always blocks regardless of any waiver name match.
            blocking.append(f"{case.get('classname')} :: {name} -- not run (status={status!r})")
            continue
        if name in ALLOWED_WAIVER_TEST_NAMES:
            waived.append(name)
            print(f"WARNING: Known, named waiver (per ADR-0003/ADR-0004): '{name}' failed/errored and is being waived, not treated as blocking.")
            continue
        blocking.append(f"{case.get('classname')} :: {name} -- {problem.get('message')}")

    if blocking:
        raise RuntimeError(
            f"LUnit gate FAIL: {len(blocking)} test case(s) failed/errored without a named waiver:\n" + "\n".join(blocking)
        )

    return {
        "outcome": "PASS",
        "totalTests": len(cases),
        "waivedTests": waived,
        "suiteCommit": suite_commit,
        # Per ADR-0003/REQ-033: "both must be recorded with every gate
        # result; neither substitutes for the other" -- this engine's own
        # release-candidate commit (the one the workflow_dispatch run was
        # triggered against), distinct from suiteCommit above (the
        # external ni/labview-icon-editor pin). #94's review found this
        # missing.
        "engineCommit": engine_commit,
        "vipmPresence": False,
    }


def validate_commit_sha(value: str, *, label: str) -> None:
    """Raises RuntimeError unless value is a full 40-character lowercase-hex
    git commit SHA. Per #94's independent verification review:
    --suite-commit was previously accepted unvalidated, so a mutable ref
    (e.g. "main") could silently enter what REQ-036 requires to be a
    pinned-commit execution."""
    if not _COMMIT_SHA_PATTERN.match(value):
        raise RuntimeError(
            f"{label} {value!r} is not a full 40-character lowercase-hex "
            "commit SHA. REQ-036 requires a pinned commit, not a mutable "
            "ref (e.g. a branch or tag name)."
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite-commit", required=True)
    parser.add_argument(
        "--engine-commit",
        default=os.environ.get("GITHUB_SHA"),
        help=(
            "This engine's own release-candidate commit under test, "
            "distinct from --suite-commit (REQ-033/ADR-0003: both must "
            "be recorded with every gate result). Defaults to "
            "$GITHUB_SHA when run from a GitHub Actions workflow."
        ),
    )
    parser.add_argument("--results-directory", required=True, type=Path)
    args = parser.parse_args()

    validate_commit_sha(args.suite_commit, label="--suite-commit")
    if not args.engine_commit:
        raise RuntimeError(
            "--engine-commit was not supplied and $GITHUB_SHA is not set -- "
            "ADR-0003/REQ-033 requires this engine's own release-candidate "
            "commit to be recorded alongside the suite commit."
        )
    validate_commit_sha(args.engine_commit, label="--engine-commit")

    results_dir = args.results_directory
    results_dir.mkdir(parents=True, exist_ok=True)
    assert_no_vipm_presence()

    get_pinned_suite_archive("ni", "labview-icon-editor", args.suite_commit, WORKSPACE)
    project = WORKSPACE / "lv_icon_editor.lvproj"
    if not project.exists():
        raise RuntimeError(f"Pinned suite commit {args.suite_commit} does not contain lv_icon_editor.lvproj.")

    report = results_dir / "lunit.xml"
    report.unlink(missing_ok=True)

    process = start_labview_headless(project, results_dir)
    try:
        completed = subprocess.run(
            [
                CLI_EXE,
                "-OperationName", "LUnit",
                "-LabVIEWPath", str(LABVIEW_EXE),
                "-PortNumber", "3363",
                "-AdditionalOperationDirectory", str(LABVIEW_ROOT / "vi.lib/Astemes/LUnit CLI"),
                "-Path", str(project),
                "-ReportPath", str(report),
                "-LogToConsole", "TRUE",
                "-Headless",
            ],
            capture_output=True,
            text=True,
        )
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        if completed.returncode != 0:
            raise RuntimeError(f"LUnit CLI failed with exit code {completed.returncode}.")

        result = validate_junit_report(report, args.suite_commit, args.engine_commit)
        (results_dir / "gate-result.json").write_text(json.dumps(result, indent=4))
        print(f"LUnit gate PASS: {result['totalTests']} test case(s), {len(result['waivedTests'])} named waiver(s).")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())
