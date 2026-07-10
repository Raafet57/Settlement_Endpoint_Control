#!/usr/bin/env python3
"""SEC-P20 blocker 3: endpoint-profile registry UI commit-status truth.

The browser runtime is unavailable in this environment, so this drives the ACTUAL
inline script from ``demo-db/index.html`` through a bounded Node + minimal-DOM/fetch
harness (``ui_commit_status_harness.mjs``) and asserts the commit-status contract:

  * a committed create/update/activate/supersede whose post-commit registry refresh
    fails must stay described as committed (with a distinct refresh warning), never
    reclassified as a rejection; and
  * a normal save success must survive ``resetProfileForm()``; and
  * a genuine mutation rejection must still be reported as rejected.

If Node is not installed the executable contract cannot run and the test is skipped
(mirroring the browser-QA guidance) rather than failing the suite.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / "ui_commit_status_harness.mjs"


class UiCommitStatusContractTests(unittest.TestCase):
    def test_commit_status_contract_holds(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node runtime unavailable; the executable UI contract cannot run")
        self.assertTrue(HARNESS.exists(), "the UI commit-status harness is missing")
        proc = subprocess.run(
            [node, str(HARNESS)],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"UI commit-status contract failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("UI_COMMIT_STATUS PASS", proc.stdout, proc.stdout)
        # The contract is claimed for create / update / activate / supersede, so the
        # harness must actually exercise the post-commit-refresh-failure path of ALL
        # four mutations -- not just create and activate. Require explicit PASS
        # markers for the update and supersede cases.
        self.assertIn(
            "[PASS] submit(update):", proc.stdout,
            f"update post-commit-refresh case is not exercised:\n{proc.stdout}",
        )
        self.assertIn(
            "[PASS] supersede:", proc.stdout,
            f"supersede post-commit-refresh case is not exercised:\n{proc.stdout}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
