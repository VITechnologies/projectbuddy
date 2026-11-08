import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "Check-RemoteReleaseTag.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class RemoteReleaseTagGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bin_dir = Path(self.temp.name) / "bin"
        self.bin_dir.mkdir()
        self.git_log = Path(self.temp.name) / "git-args"
        fake_git = self.bin_dir / "git"
        fake_git.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" > \"$FAKE_GIT_LOG\"\n"
            "printf '%s' \"$FAKE_GIT_STDOUT\"\n"
            "printf '%s' \"$FAKE_GIT_STDERR\" >&2\n"
            "exit \"$FAKE_GIT_STATUS\"\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_guard(self, status, stdout="", stderr=""):
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(self.bin_dir) + os.pathsep + env["PATH"],
                "FAKE_GIT_LOG": str(self.git_log),
                "FAKE_GIT_STATUS": str(status),
                "FAKE_GIT_STDOUT": stdout,
                "FAKE_GIT_STDERR": stderr,
            }
        )
        return subprocess.run(
            ["bash", str(GUARD), "v1.2.3", "origin"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_documented_no_matching_ref_exit_is_absence(self):
        result = self.run_guard(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is absent", result.stdout)
        self.assertEqual(
            self.git_log.read_text(encoding="utf-8").strip(),
            "ls-remote --exit-code --tags origin refs/tags/v1.2.3",
        )

    def test_existing_tag_blocks_creation(self):
        result = self.run_guard(0, "a" * 40 + "\trefs/tags/v1.2.3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stdout)

    def test_remote_failure_is_not_absence_and_is_redacted(self):
        result = self.run_guard(128, stderr="fatal: private remote URL unavailable")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exit 128", result.stdout)
        self.assertNotIn("private remote URL", result.stdout + result.stderr)

    def test_unexpected_exit_two_with_stderr_is_not_absence(self):
        result = self.run_guard(2, stderr="fatal: transport error")
        self.assertEqual(result.returncode, 2)
        self.assertIn("failed", result.stdout)

    def test_other_nonzero_exit_is_not_absence(self):
        result = self.run_guard(1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("exit 1", result.stdout)

    def test_malformed_success_output_is_indeterminate(self):
        for output in (
            "",
            "not-an-object\trefs/tags/v1.2.3",
            "a" * 40 + "\trefs/tags/v1.2.4",
            "a" * 40 + "\trefs/tags/v1.2.3\n" + "b" * 40 + "\trefs/tags/v1.2.3",
        ):
            with self.subTest(output=output):
                result = self.run_guard(0, output)
                self.assertEqual(result.returncode, 2)
                self.assertIn("malformed or ambiguous", result.stdout)

    def test_invalid_version_does_not_query_git(self):
        env = os.environ.copy()
        env["PATH"] = str(self.bin_dir) + os.pathsep + env["PATH"]
        result = subprocess.run(
            ["bash", str(GUARD), "not-a-version", "origin"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.git_log.exists())

    def test_release_workflow_runs_guard_before_release_creation(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        guard = text.index("./scripts/Check-RemoteReleaseTag.sh")
        release = text.index("name: Create Release")
        self.assertLess(guard, release)


if __name__ == "__main__":
    unittest.main()
