from __future__ import annotations

import unittest

from covalent.application.errors import (
    DelegateConcurrentModificationError, DelegateOwnershipError, DelegateQuotaError,
    DelegateRunGoneError, DelegateRunNotFoundError, DelegateTransitionError,
)
from covalent.infra.settings import AppSettings


class DelegateSettingsTests(unittest.TestCase):
    def test_defaults_flag_off(self) -> None:
        settings = AppSettings()
        self.assertFalse(settings.stateful_delegates_enabled)
        self.assertEqual(settings.delegate_max_depth, 3)
        self.assertEqual(settings.delegate_max_messages_per_run, 200)

    def test_env_prefix_maps(self) -> None:
        import os
        os.environ["AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED"] = "true"
        try:
            self.assertTrue(AppSettings().stateful_delegates_enabled)
        finally:
            del os.environ["AGENT_FRAMEWORK_STATEFUL_DELEGATES_ENABLED"]


class DelegateErrorTests(unittest.TestCase):
    def test_error_status_codes(self) -> None:
        cases = [
            (DelegateRunNotFoundError("x"), 404), (DelegateOwnershipError("x"), 403),
            (DelegateTransitionError("x"), 409), (DelegateConcurrentModificationError("x"), 409),
            (DelegateQuotaError("x"), 429), (DelegateRunGoneError("x"), 409),
        ]
        for error, code in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(error.status_code, code)
                self.assertEqual(error.message, "x")


if __name__ == "__main__":
    unittest.main()
