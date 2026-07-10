#!/usr/bin/env python3
"""Canonical quality gate for the Settlement Endpoint Control Tower (SEC-O20).

One command, standard library only, run identically in local development and in
CI (`.github/workflows/ci.yml`):

    python3 ci.py

It reproduces every canonical check and fails closed if any gate fails:

    1. static QA            demo/qa_static_demo.py            -> STATIC_QA PASS
    2. demo-db unit tests   unittest discover demo-db         -> OK
    3. DB smoke             demo-db/smoke.py                  -> SMOKE PASS
    4. Python validation    compile every tracked *.py
    5. JSON validation      json.load every tracked *.json
    6. HTML validation      lenient html.parser + doc basics
    7. secret scan          high-confidence credential patterns
    8. retired framing      retired event-organizer references
    9. workflow validation  exact canonical-contract check of the CI workflow

Design boundaries:

* Standard library only; no new dependency is added for linting. The workflow
  gate does not use any YAML parser (optional or otherwise): the CI workflow is
  intentionally tiny and fixed, so it is validated against an exact canonical
  line contract that is fully deterministic in every environment -- including a
  clean CI runner with no PyYAML installed.
* The scan surface is tracked files plus nonignored untracked candidates
  (`git ls-files` and `git ls-files --others --exclude-standard`), so `.git` and
  ignored generated DB/cache artifacts are never scanned while new source files
  are covered before commit.
* The secret scan targets high-confidence credential material/assignments, never
  prints a matched value, and reports only ``path:line: pattern``.
* Retired-framing covers current shipped/product-facing text and code but
  excludes the immutable product-review history under ``docs/product-shaping/``;
  the retired terms are assembled at runtime (split-string) so the literal
  labels never appear in this scanner's source.
* The secret and retired-framing scans cover every eligible text file including
  this script itself; its detection patterns/terms are anchored and split so
  they never self-match, so no file is a blind spot.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# Immutable product-review evidence: excluded from the shipped-product framing
# gate so historical discovery text cannot fail the current-framing check.
IMMUTABLE_PREFIX = "docs/product-shaping/"


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def scan_files() -> list:
    """The version-controlled file surface: tracked files plus not-yet-committed
    files that are not gitignored.

    Using ``git`` keeps ``.git`` itself, generated DB state, and cache artifacts
    out of the scan (they are gitignored), while ``--others --exclude-standard``
    still validates new files before they are committed -- so a local run covers
    the same surface CI scans after checkout.
    """
    def run_git(args: list) -> list:
        proc = subprocess.run(["git"] + args, cwd=str(ROOT), text=True, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError("git " + " ".join(args) + " failed; the quality gate requires a git checkout")
        return [line for line in proc.stdout.splitlines() if line]

    names = set(run_git(["ls-files"]))
    names.update(run_git(["ls-files", "--others", "--exclude-standard"]))
    return [(ROOT / name) for name in sorted(names)]


def read_text_or_none(path: Path):
    """UTF-8 text, or None for a binary/undecodable file (skipped by scans)."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


# ----------------------------------------------------------------------
# Subprocess gates (existing canonical checks). Their PASS tokens are echoed
# so their individual PASS remains visible in the combined output.
# ----------------------------------------------------------------------


def run_command_gate(name: str, argv: list, expect_token: str = None) -> tuple:
    proc = subprocess.run(
        [sys.executable] + argv, cwd=str(ROOT), text=True, capture_output=True
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        tail = "\n".join(combined.strip().splitlines()[-15:])
        return False, f"{name} exited {proc.returncode}\n{tail}"
    if expect_token and expect_token not in combined:
        return False, f"{name} did not report {expect_token!r}"
    token_line = ""
    if expect_token:
        for line in combined.splitlines():
            if expect_token in line:
                token_line = line.strip()
                break
    return True, token_line or f"{name} ok"


# ----------------------------------------------------------------------
# File gates.
# ----------------------------------------------------------------------


def gate_python(files: list) -> tuple:
    pyfiles = [f for f in files if f.suffix == ".py"]
    failures = []
    for path in pyfiles:
        text = read_text_or_none(path)
        if text is None:
            failures.append(f"{rel(path)}: not readable as utf-8")
            continue
        try:
            compile(text, str(path), "exec")
        except SyntaxError as exc:
            failures.append(f"{rel(path)}: syntax error at line {exc.lineno}")
    if failures:
        return False, "; ".join(failures)
    return True, f"compiled {len(pyfiles)} tracked python file(s)"


def gate_json(files: list) -> tuple:
    jsonfiles = [f for f in files if f.suffix == ".json"]
    failures = []
    for path in jsonfiles:
        text = read_text_or_none(path)
        if text is None:
            failures.append(f"{rel(path)}: not readable as utf-8")
            continue
        try:
            json.loads(text)
        except ValueError as exc:
            failures.append(f"{rel(path)}: invalid JSON ({type(exc).__name__})")
    if failures:
        return False, "; ".join(failures)
    return True, f"validated {len(jsonfiles)} tracked json file(s)"


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags = set()

    def handle_starttag(self, tag, attrs) -> None:
        self.tags.add(tag)

    def handle_startendtag(self, tag, attrs) -> None:
        self.tags.add(tag)


# Lenient structural parse plus required document basics. This does NOT claim
# full HTML standards conformance -- only that html.parser processes the file
# and the minimum document scaffolding is present.
_HTML_MARKERS = ("<!doctype", "<html", "<head", "<title", "<body", "</html>")
_HTML_TAGS = ("html", "head", "title", "body")


def gate_html(files: list) -> tuple:
    htmlfiles = [f for f in files if f.suffix in (".html", ".htm")]
    failures = []
    for path in htmlfiles:
        text = read_text_or_none(path)
        if text is None:
            failures.append(f"{rel(path)}: not readable as utf-8")
            continue
        parser = _StructureParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception as exc:  # noqa: BLE001 - any parse error fails the gate
            failures.append(f"{rel(path)}: html.parser error ({type(exc).__name__})")
            continue
        low = text.lower()
        missing = [m for m in _HTML_MARKERS if m not in low]
        if missing:
            failures.append(f"{rel(path)}: missing document basics {missing}")
        missing_tags = [t for t in _HTML_TAGS if t not in parser.tags]
        if missing_tags:
            failures.append(f"{rel(path)}: no start tag(s) {missing_tags}")
    if failures:
        return False, "; ".join(failures)
    return True, f"structural-parsed {len(htmlfiles)} tracked html file(s)"


# High-confidence secret patterns. Prefix/format anchored (very low false-
# positive) plus a keyword-assignment rule whose value is a single-line, non-
# whitespace, >=12-char literal. A matched value is NEVER printed.
SECRET_PATTERNS = [
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[porsu]_[0-9A-Za-z]{36}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_secret_key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b")),
    ("pem_private_key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|apikey|access[_-]?key|"
            r"secret[_-]?key|client[_-]?secret|auth[_-]?token|access[_-]?token)\b"
            r"\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']"
        ),
    ),
]


def gate_secrets(files: list) -> tuple:
    # Every eligible text file is scanned, including this scanner itself: its
    # detection patterns are anchored/split so they do not self-match. This is a
    # security gate, so it FAILS CLOSED: an enumerated file that cannot be read
    # as UTF-8 is an unscannable blind spot, and a scan that covers zero readable
    # files proves nothing -- both fail. Error output names the path and error
    # category only, never file contents or matched values.
    hits = []
    unreadable = []
    scanned = 0
    for path in files:
        text = read_text_or_none(path)
        if text is None:
            unreadable.append(f"{rel(path)}: not readable as utf-8")
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    # Record location + pattern name ONLY; never the value.
                    hits.append(f"{rel(path)}:{lineno}: {name}")
    if unreadable:
        return False, "unscannable (non-utf-8/unreadable) file(s): " + "; ".join(unreadable)
    if scanned == 0:
        return False, "secret scan covered zero readable files (fail-closed)"
    if hits:
        return False, "high-confidence secret pattern(s): " + "; ".join(hits)
    return True, f"no high-confidence secrets in {scanned} eligible text file(s)"


# Retired event-organizer references, assembled at runtime so the literal labels
# never appear in this source (matching demo/qa_static_demo.py's split pattern).
RETIRED_TERMS = ["sw" + "ift", "ap" + "ix", "hack" + "athon"]
RETIRED_RE = re.compile("|".join(re.escape(t) for t in RETIRED_TERMS), re.IGNORECASE)


def gate_retired_framing(files: list) -> tuple:
    # Covers every eligible shipped/product-facing file, including this scanner
    # (its retired terms are split so they never appear literally); only the
    # immutable product-review history is excluded. Like the secret gate this
    # FAILS CLOSED: an enumerated file (outside the immutable history) that
    # cannot be read as UTF-8 is an unscannable blind spot, and a scan covering
    # zero readable files proves nothing -- both fail. Error output names the
    # path and error category only, never file contents or matched labels.
    hits = []
    unreadable = []
    scanned = 0
    for path in files:
        relpath = rel(path)
        if relpath.startswith(IMMUTABLE_PREFIX):  # immutable discovery evidence
            continue
        text = read_text_or_none(path)
        if text is None:
            unreadable.append(f"{relpath}: not readable as utf-8")
            continue
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            if RETIRED_RE.search(line):
                # Report location only; do not echo the retired label.
                hits.append(f"{relpath}:{lineno}")
    if unreadable:
        return False, "unscannable (non-utf-8/unreadable) file(s): " + "; ".join(unreadable)
    if scanned == 0:
        return False, "retired-framing scan covered zero readable files (fail-closed)"
    if hits:
        return False, "retired event-organizer reference(s) at: " + "; ".join(hits)
    return True, f"no retired framing in {scanned} shipped/product-facing file(s)"


# The CI workflow is intentionally tiny and fixed, so validation is an EXACT
# canonical shape check: standard-library-only and fully deterministic in every
# environment (no optional YAML parser whose absence changes the verdict). We
# strip full-line/inline comments and blank lines, normalize trailing
# whitespace, then require the remaining significant lines -- including their
# exact indentation -- to equal this canonical contract. That rejects malformed
# indentation/structure, extra jobs/steps/run commands, impostor actions that
# merely share an approved prefix, services, secrets, and write/deploy/publish
# permissions, because any of them changes the significant-line sequence.
CANONICAL_WORKFLOW_LINES = [
    "name: CI",
    "on:",
    "  push:",
    "    branches: [main]",
    "  pull_request:",
    "    branches: [main]",
    "permissions:",
    "  contents: read",
    "jobs:",
    "  quality-gate:",
    "    runs-on: ubuntu-latest",
    "    steps:",
    "      - name: Check out repository",
    "        uses: actions/checkout@v4",
    "      - name: Set up Python 3.9",
    "        uses: actions/setup-python@v5",
    "        with:",
    "          python-version: '3.9'",
    "      - name: Run canonical quality gate",
    "        run: python3 ci.py",
]


def _significant_workflow_lines(text: str) -> list:
    """Comment-stripped, blank-line-free, right-trimmed lines (indentation kept).

    Comments are inert in the workflow, so a descriptive ``# ...`` comment is
    dropped rather than treated as structure; everything else must match the
    canonical contract byte-for-byte, indentation included.
    """
    lines = []
    for raw in text.splitlines():
        stripped = re.sub(r"(?:^|\s)#.*$", "", raw).rstrip()
        if stripped:
            lines.append(stripped)
    return lines


def gate_workflow() -> tuple:
    if not WORKFLOW.exists():
        return False, "missing .github/workflows/ci.yml"
    text = read_text_or_none(WORKFLOW)
    if text is None:
        return False, "workflow not readable as utf-8"

    actual = _significant_workflow_lines(text)
    if actual != CANONICAL_WORKFLOW_LINES:
        n = min(len(actual), len(CANONICAL_WORKFLOW_LINES))
        idx = next((i for i in range(n) if actual[i] != CANONICAL_WORKFLOW_LINES[i]), n)
        # Report the deviating significant-line number and the expected shape
        # only; do not echo the (attacker-controlled) actual line content.
        return False, (
            "workflow deviates from the exact minimal contract at significant "
            f"line {idx + 1} (expected {len(CANONICAL_WORKFLOW_LINES)} lines, "
            f"got {len(actual)})"
        )
    return True, f"validated exact canonical contract ({len(actual)} significant lines)"


def main() -> int:
    print("== Settlement Endpoint Control Tower quality gate ==")
    try:
        files = scan_files()
    except RuntimeError as exc:
        print(f"[FAIL] enumerate: {exc}")
        return 1

    gates = [
        ("static_qa", lambda: run_command_gate("static_qa", ["demo/qa_static_demo.py"], "STATIC_QA PASS")),
        ("unit_tests", lambda: run_command_gate("unit_tests", ["-m", "unittest", "discover", "-s", "demo-db", "-p", "test_*.py"])),
        ("db_smoke", lambda: run_command_gate("db_smoke", ["demo-db/smoke.py"], "SMOKE PASS")),
        ("python", lambda: gate_python(files)),
        ("json", lambda: gate_json(files)),
        ("html", lambda: gate_html(files)),
        ("secrets", lambda: gate_secrets(files)),
        ("retired_framing", lambda: gate_retired_framing(files)),
        ("workflow", gate_workflow),
    ]

    results = []
    for name, run in gates:
        try:
            ok, info = run()
        except Exception as exc:  # noqa: BLE001 - any gate error fails closed
            ok, info = False, f"gate raised {type(exc).__name__}: {exc}"
        results.append((name, ok, info))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {info}")

    failed = [name for name, ok, _ in results if not ok]
    print("=" * 52)
    if failed:
        print(f"QUALITY GATE FAILED ({len(failed)}/{len(results)} failed): {', '.join(failed)}")
        return 1
    print(f"QUALITY GATE PASS ({len(results)}/{len(results)} gates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
