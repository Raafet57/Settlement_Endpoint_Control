#!/usr/bin/env python3
"""Focused tests for the three SEC-O20 CI-gate blockers in ``ci.py``.

These pin fail-closed behaviour that a clean GitHub runner (no PyYAML) must
enforce:

1. The secret and retired-framing text/security gates must FAIL CLOSED when an
   enumerated file cannot be read as UTF-8, and when the scan covers zero
   readable files. Error output may name the path and an error category only --
   never file contents or matched values.
2. Workflow validation must be deterministic and standard-library-only, so it
   behaves identically whether or not PyYAML is importable, and rejects
   malformed indentation/structure.
3. The workflow validator must enforce the exact minimal contract: an extra
   ``run:`` step is rejected, and an impostor action whose id merely starts with
   an approved prefix (``actions/checkout-evil@v4``) is rejected, while the exact
   current workflow passes.

All fixtures are synthetic, local-only OS temp files (cleaned up), and contain no
realistic credential material.
"""
from __future__ import annotations

import builtins
import importlib
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ci  # noqa: E402  (repo-root module, imported after path setup)

WORKFLOW_FILE = ROOT / ".github" / "workflows" / "ci.yml"

# A syntactically valid but obviously synthetic secret-shaped assignment,
# assembled at runtime so no fixed credential-shaped literal is committed here.
SYNTHETIC_SECRET_LINE = "api_key" + " = " + '"' + ("z" * 20) + '"'

# The canonical workflow, indentation flattened to column 0. It still contains
# every substring a lenient scanner keys on, so a non-strict validator would
# accept it -- but the indentation is malformed, so a strict shape check rejects
# it. Used to prove determinism + malformed-structure rejection.
FLATTENED_WORKFLOW = "\n".join(
    [
        "name: CI",
        "on:",
        "push:",
        "branches: [main]",
        "pull_request:",
        "branches: [main]",
        "permissions:",
        "contents: read",
        "jobs:",
        "quality-gate:",
        "runs-on: ubuntu-latest",
        "steps:",
        "- name: Check out repository",
        "uses: actions/checkout@v4",
        "- name: Set up Python 3.9",
        "uses: actions/setup-python@v5",
        "with:",
        "python-version: '3.9'",
        "- name: Run canonical quality gate",
        "run: python3 ci.py",
    ]
)


@contextmanager
def temp_files(*specs):
    """Yield real Paths to synthetic temp files. Each spec is (suffix, bytes)."""
    paths = []
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        for i, (suffix, data) in enumerate(specs):
            p = base / f"f{i}{suffix}"
            p.write_bytes(data)
            paths.append(p)
        yield paths


@contextmanager
def no_pyyaml():
    """Make ``import yaml`` fail and reload ci, to emulate a clean CI runner."""
    saved = sys.modules.get("yaml")
    sys.modules.pop("yaml", None)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("yaml blocked for test")
        return real_import(name, *args, **kwargs)

    with mock.patch.object(builtins, "__import__", fake_import):
        importlib.reload(ci)
        try:
            yield
        finally:
            pass
    # Restore a clean ci import and the real yaml module state.
    if saved is not None:
        sys.modules["yaml"] = saved
    importlib.reload(ci)


@contextmanager
def patched_workflow(text: str):
    """Point ci.WORKFLOW at a synthetic temp workflow file with ``text``."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ci.yml"
        p.write_text(text, encoding="utf-8")
        with mock.patch.object(ci, "WORKFLOW", p):
            yield


# ---------------------------------------------------------------------------
# Blocker 1: secret / retired-framing gates fail closed.
# ---------------------------------------------------------------------------


class TextGateFailClosedTests(unittest.TestCase):
    NON_UTF8 = b"harmless marker text \xff\xfe binary tail"

    def test_secrets_unreadable_input_fails_without_disclosure(self):
        with temp_files((".bin", self.NON_UTF8)) as (p,):
            ok, info = ci.gate_secrets([p])
        self.assertFalse(ok, "non-UTF-8 file must fail the secret gate closed")
        self.assertIn(p.name, info)  # path may be reported
        self.assertNotIn("harmless marker text", info)  # never disclose content
        self.assertNotIn("binary tail", info)

    def test_secrets_zero_input_fails(self):
        ok, info = ci.gate_secrets([])
        self.assertFalse(ok, "a zero-file secret scan must not report PASS")

    def test_retired_unreadable_input_fails_without_disclosure(self):
        with temp_files((".bin", self.NON_UTF8)) as (p,):
            ok, info = ci.gate_retired_framing([p])
        self.assertFalse(ok, "non-UTF-8 file must fail the retired-framing gate closed")
        self.assertIn(p.name, info)
        self.assertNotIn("harmless marker text", info)

    def test_retired_zero_input_fails(self):
        ok, info = ci.gate_retired_framing([])
        self.assertFalse(ok, "a zero-file retired-framing scan must not report PASS")

    def test_secrets_readable_clean_file_passes(self):
        with temp_files((".txt", b"nothing sensitive here\n")) as (p,):
            ok, info = ci.gate_secrets([p])
        self.assertTrue(ok, "a readable, clean file must still pass")

    def test_secrets_hit_reports_location_not_value(self):
        payload = ("x = 1\n" + SYNTHETIC_SECRET_LINE + "\n").encode("utf-8")
        with temp_files((".txt", payload)) as (p,):
            ok, info = ci.gate_secrets([p])
        self.assertFalse(ok)
        self.assertNotIn("z" * 20, info)  # matched value never printed


# ---------------------------------------------------------------------------
# Blocker 2 + 3: deterministic, stdlib-only, exact-contract workflow gate.
# ---------------------------------------------------------------------------


class WorkflowGateTests(unittest.TestCase):
    def test_exact_current_workflow_passes(self):
        ok, info = ci.gate_workflow()
        self.assertTrue(ok, f"the committed canonical workflow must pass: {info}")

    def test_malformed_workflow_fails_pyyaml_absent_irrelevant(self):
        with no_pyyaml():
            with patched_workflow(FLATTENED_WORKFLOW):
                ok, info = ci.gate_workflow()
            self.assertFalse(ok, "malformed-indentation workflow must fail closed")
            # Determinism: the exact canonical text still passes without PyYAML.
            with patched_workflow(WORKFLOW_FILE.read_text(encoding="utf-8")):
                ok2, info2 = ci.gate_workflow()
            self.assertTrue(ok2, f"canonical workflow must pass without PyYAML: {info2}")

    def test_extra_run_step_fails(self):
        base = WORKFLOW_FILE.read_text(encoding="utf-8").rstrip("\n")
        tampered = base + "\n      - name: Sneak\n        run: echo pwned\n"
        with patched_workflow(tampered):
            ok, info = ci.gate_workflow()
        self.assertFalse(ok, "an extra run step must be rejected")

    def test_approved_prefix_impostor_action_fails(self):
        base = WORKFLOW_FILE.read_text(encoding="utf-8")
        tampered = base.replace("actions/checkout@v4", "actions/checkout-evil@v4")
        self.assertIn("actions/checkout-evil@v4", tampered)
        with patched_workflow(tampered):
            ok, info = ci.gate_workflow()
        self.assertFalse(ok, "an impostor action sharing an approved prefix must be rejected")

    def test_write_permission_variant_fails(self):
        base = WORKFLOW_FILE.read_text(encoding="utf-8")
        tampered = base.replace("contents: read", "contents: write")
        with patched_workflow(tampered):
            ok, info = ci.gate_workflow()
        self.assertFalse(ok, "a write permission must be rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
