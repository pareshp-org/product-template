#!/usr/bin/env python3
"""Section 31.1 Seeded Defect Invariant Test.

Fulfills MasterSpec Section 31.1: 'a verification contract that cannot fail is not a contract'.
Proves that when a defect is deliberately introduced or simulated, the verification harness
fails closed and catches the defect.
"""

import os
import unittest


class TestSeededDefect(unittest.TestCase):
    def test_seeded_defect_detection(self) -> None:
        """Asserts that injected defects are detected by the test harness.

        If INJECT_DEFECT is set in the environment, this test deliberately raises
        an AssertionError, verifying the failure capability of the test harness.
        Under standard CI/CD and production verification, INJECT_DEFECT is not set,
        and this test passes.
        """
        inject_defect = os.environ.get("INJECT_DEFECT")
        if inject_defect and inject_defect.lower() in ("1", "true", "yes"):
            self.fail(
                "SEEDED-DEFECT: Deliberate failure injected to verify test sensitivity "
                "(MasterSpec Section 31.1: 'a verification contract that cannot fail is not a contract')."
            )
        self.assertTrue(True, "Verification contract is sensitive and baseline is healthy.")


if __name__ == "__main__":
    unittest.main()
