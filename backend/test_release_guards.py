from __future__ import annotations

import unittest

from release_guards import run_all_checks


class DashboardReleaseGuardTests(unittest.TestCase):
    def test_frontend_backend_and_runtime_release_guards(self):
        errors = run_all_checks()
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
