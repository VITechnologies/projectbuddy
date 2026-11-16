import contextlib
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class InvalidCredentialsError(Exception):
    pass


def load_provisioner():
    winrm_stub = types.ModuleType("winrm")
    winrm_stub.exceptions = SimpleNamespace(InvalidCredentialsError=InvalidCredentialsError)
    source = Path(__file__).parents[2] / "vm/windows-labview/provision-windows-guest.py"
    spec = importlib.util.spec_from_file_location("provision_windows_guest_test", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"winrm": winrm_stub}):
        spec.loader.exec_module(module)
    return module


class WinRmAuthExitTests(unittest.TestCase):
    def test_invalid_credentials_return_dedicated_status_without_echoing_secret(self):
        module = load_provisioner()
        secret = "never-print-this-guest-password"

        def reject_credentials(_args):
            raise InvalidCredentialsError(secret)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = module.invoke_command(SimpleNamespace(func=reject_credentials))

        self.assertEqual(3, result)
        self.assertIn("WinRM rejected the guest credentials", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def test_other_errors_are_not_misclassified_as_authentication(self):
        module = load_provisioner()

        def fail_transport(_args):
            raise TimeoutError("listener unavailable")

        with self.assertRaisesRegex(TimeoutError, "listener unavailable"):
            module.invoke_command(SimpleNamespace(func=fail_transport))

    def test_successful_command_status_is_preserved(self):
        module = load_provisioner()
        result = module.invoke_command(SimpleNamespace(func=lambda _args: 0))
        self.assertEqual(0, result)


if __name__ == "__main__":
    unittest.main()