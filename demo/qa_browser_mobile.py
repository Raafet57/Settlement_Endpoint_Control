#!/usr/bin/env python3
"""Browser and mobile smoke for the static and DB reference demos."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "demo"
DB_DIR = ROOT / "demo-db"
ARTIFACT_DIR = ROOT / ".artifacts" / "browser-smoke"
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 1000, "is_mobile": False},
    "mobile": {"width": 390, "height": 844, "is_mobile": True},
}
STATIC_SCENARIO_VERDICTS = {
    "blocked": "TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED",
    "refreshed": "TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED",
    "authority": "AUTHORITY_EXPIRED_MANUAL_HOLD",
}
STATIC_ROLE_NOTES = {
    "operator": "Ops analyst view",
    "risk": "Risk reviewer view",
    "approver": "Four-eyes approver view",
}
STATIC_BLOCKED_REPAIR_ITEMS = ("wallet allowlist", "endpoint-control payload", "authority evidence")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as res:  # noqa: S310 - localhost smoke only
                if res.status < 500:
                    return
        except Exception as exc:  # pragma: no cover - error text reported below
            last_error = exc
            time.sleep(0.15)
    raise RuntimeError(f"HTTP_NOT_READY {url} {last_error}")


def start_static(port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=DEMO_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def start_db(port: int) -> subprocess.Popen[str]:
    subprocess.run([sys.executable, "seed.py"], cwd=DB_DIR, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    proc = subprocess.Popen(
        [sys.executable, "app.py", "--host", "127.0.0.1", "--port", str(port)],
        cwd=DB_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def stop(proc: subprocess.Popen[str] | None) -> None:
    if not proc:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=4)


def page_text(page) -> str:
    return page.locator("body").inner_text(timeout=3000)


def assert_no_console_or_page_errors(errors: list[str], label: str) -> None:
    fatal = [e for e in errors if "favicon" not in e.lower()]
    if fatal:
        raise AssertionError(f"BROWSER_ERRORS {label} {fatal[:5]}")


def assert_no_bad_text(page, label: str) -> None:
    text = page_text(page)
    bad = [token for token in ["undefined", "null", "NaN", "[object Object]"] if token in text]
    if bad:
        raise AssertionError(f"BAD_VISIBLE_TEXT {label} {bad}")


def assert_no_horizontal_overflow(page, label: str) -> None:
    metrics = page.evaluate(
        """() => ({
          innerWidth: window.innerWidth,
          docScrollWidth: document.documentElement.scrollWidth,
          bodyScrollWidth: document.body.scrollWidth
        })"""
    )
    limit = metrics["innerWidth"] + 2
    if metrics["docScrollWidth"] > limit or metrics["bodyScrollWidth"] > limit:
        raise AssertionError(f"HORIZONTAL_OVERFLOW {label} {json.dumps(metrics, sort_keys=True)}")


def tab_states(page) -> list:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('.tab')).map(btn => ({
          screen: btn.dataset.screen,
          selected: btn.getAttribute('aria-selected'),
          tabindex: btn.tabIndex,
          focused: document.activeElement === btn,
        }))"""
    )


def assert_active_tab(page, expected_screen: str, label: str, step: str) -> None:
    states = tab_states(page)
    selected = [s for s in states if s["selected"] == "true"]
    if len(selected) != 1 or selected[0]["screen"] != expected_screen:
        raise AssertionError(f"TAB_SELECTION {label} {step} {states}")
    roving_ok = all(s["tabindex"] == (0 if s["screen"] == expected_screen else -1) for s in states)
    if not roving_ok:
        raise AssertionError(f"TAB_ROVING_TABINDEX {label} {step} {states}")
    if not selected[0]["focused"]:
        raise AssertionError(f"TAB_FOCUS {label} {step} {states}")


def check_tab_keyboard(page, label: str) -> None:
    profile = page.locator('[data-screen="profile"]')
    profile.click()
    profile.focus()
    assert_active_tab(page, "profile", label, "focus-profile")
    page.keyboard.press("ArrowRight")
    assert_active_tab(page, "validation", label, "arrow-right")
    page.keyboard.press("End")
    assert_active_tab(page, "audit", label, "end")
    page.keyboard.press("Home")
    assert_active_tab(page, "profile", label, "home")
    page.keyboard.press("ArrowLeft")
    assert_active_tab(page, "audit", label, "arrow-left-wrap")
    page.keyboard.press("ArrowRight")
    assert_active_tab(page, "profile", label, "arrow-right-wrap")


def theme_state(page) -> dict:
    return page.evaluate(
        """() => {
          const toggle = document.getElementById('themeToggle');
          return {
            dark: document.documentElement.classList.contains('dark'),
            label: toggle.getAttribute('aria-label'),
            pressed: toggle.getAttribute('aria-pressed'),
            visible: toggle.innerText.trim(),
          };
        }"""
    )


def assert_theme_announced(state: dict, label: str, step: str) -> None:
    expected_label = "Dark mode"
    expected_pressed = "true" if state["dark"] else "false"
    if state["label"] != expected_label or state["visible"] != expected_label or state["pressed"] != expected_pressed:
        raise AssertionError(f"THEME_ARIA {label} {step} {state}")


def check_theme_toggle(page, label: str) -> None:
    initial = theme_state(page)
    assert_theme_announced(initial, label, "initial")
    page.locator("#themeToggle").click()
    flipped = theme_state(page)
    assert_theme_announced(flipped, label, "flipped")
    if flipped["dark"] == initial["dark"]:
        raise AssertionError(f"THEME_DID_NOT_FLIP {label} {initial} {flipped}")
    page.locator("#themeToggle").click()
    restored = theme_state(page)
    assert_theme_announced(restored, label, "restored")
    if restored["dark"] != initial["dark"]:
        raise AssertionError(f"THEME_DID_NOT_RESTORE {label} {initial} {restored}")


def check_scenarios_and_roles(page, label: str) -> None:
    for scenario, verdict in STATIC_SCENARIO_VERDICTS.items():
        page.select_option("#scenarioSelect", scenario)
        actual = page.locator("#receiptVerdict").inner_text(timeout=3000).strip()
        if actual != verdict:
            raise AssertionError(f"SCENARIO_VERDICT {label} {scenario} expected {verdict!r} got {actual!r}")
    page.select_option("#scenarioSelect", "blocked")
    page.locator('[data-screen="decision"]').click()
    repair = page.locator("#repairText").inner_text(timeout=3000)
    missing_repairs = [item for item in STATIC_BLOCKED_REPAIR_ITEMS if item not in repair]
    if missing_repairs:
        raise AssertionError(f"BLOCKED_REPAIR_TEXT {label} missing {missing_repairs} in {repair!r}")
    page.locator('[data-screen="validation"]').click()
    for role, note_prefix in STATIC_ROLE_NOTES.items():
        page.select_option("#roleSelect", role)
        note = page.locator("#roleNote").inner_text(timeout=3000)
        if note_prefix not in note:
            raise AssertionError(f"ROLE_NOTE {label} {role} expected {note_prefix!r} in {note!r}")


def check_replay(page, label: str) -> None:
    # Start from a non-default state to prove replay resets scenario and role.
    page.select_option("#scenarioSelect", "refreshed")
    page.select_option("#roleSelect", "risk")
    started = time.monotonic()
    page.get_by_role("button", name="Replay the control moment").first.click()
    page.wait_for_function(
        "() => document.getElementById('walkthroughBadge').textContent.trim() === 'step 2 of 4'",
        timeout=3000,
    )
    intermediate_elapsed = time.monotonic() - started
    intermediate = page.evaluate(
        """() => ({
          badge: document.getElementById('walkthroughBadge').textContent.trim(),
          screen: window.__settlementDemo.appState.screen,
          validationActive: document.getElementById('view-validation').classList.contains('active'),
        })"""
    )
    if intermediate != {"badge": "step 2 of 4", "screen": "validation", "validationActive": True}:
        raise AssertionError(f"REPLAY_INTERMEDIATE_STATE {label} {intermediate}")
    if intermediate_elapsed < 0.75:
        raise AssertionError(f"REPLAY_NOT_TIMED {label} reached step 2 in {intermediate_elapsed:.3f}s")
    page.wait_for_function(
        "() => document.getElementById('walkthroughBadge').textContent.trim() === 'step 4 of 4'",
        timeout=8000,
    )
    state = page.evaluate(
        """() => ({
          scenario: window.__settlementDemo.appState.scenario,
          role: window.__settlementDemo.appState.role,
          screen: window.__settlementDemo.appState.screen,
          auditActive: document.getElementById('view-audit').classList.contains('active'),
        })"""
    )
    if state != {"scenario": "blocked", "role": "operator", "screen": "audit", "auditActive": True}:
        raise AssertionError(f"REPLAY_STATE {label} {state}")


def smoke_static(context, base_url: str, viewport_name: str) -> dict:
    errors: list[str] = []
    label = f"static-{viewport_name}"
    page = context.new_page()
    page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type in {"error", "warning"} else None)
    page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
    page.goto(base_url, wait_until="networkidle")
    page.get_by_role("heading", name="A wallet address is not a settlement instruction.").wait_for(timeout=5000)
    page.locator('[data-screen="profile"]').click()
    page.locator('[data-screen="validation"]').click()
    page.get_by_text("Endpoint pre-validation").wait_for(timeout=3000)
    page.locator('[data-screen="decision"]').click()
    page.get_by_text("Policy route decision").wait_for(timeout=3000)
    page.locator('[data-screen="audit"]').click()
    page.get_by_text("Evidence and audit pack").wait_for(timeout=3000)
    check_tab_keyboard(page, label)
    check_scenarios_and_roles(page, label)
    check_theme_toggle(page, label)
    check_replay(page, label)
    with page.expect_download(timeout=3000) as download_info:
        page.get_by_role("button", name="Download synthetic evidence JSON").click()
    download = download_info.value
    target = ARTIFACT_DIR / f"static-{viewport_name}-evidence.json"
    download.save_as(target)
    evidence = json.loads(target.read_text(encoding="utf-8"))
    if evidence.get("boundary") != "synthetic_data_only_no_external_network_calls":
        raise AssertionError(f"STATIC_EVIDENCE_BOUNDARY {evidence.get('boundary')}")
    if evidence.get("verdict") != STATIC_SCENARIO_VERDICTS["blocked"]:
        raise AssertionError(f"STATIC_EVIDENCE_VERDICT {evidence.get('verdict')}")
    assert_no_bad_text(page, label)
    assert_no_horizontal_overflow(page, label)
    assert_no_console_or_page_errors(errors, label)
    shot = ARTIFACT_DIR / f"static-{viewport_name}.png"
    page.screenshot(path=str(shot), full_page=True)
    page.close()
    return {"surface": "static", "viewport": viewport_name, "screenshot": str(shot.relative_to(ROOT)), "download": str(target.relative_to(ROOT)), "verdict": evidence.get("verdict")}


def smoke_db(context, base_url: str, viewport_name: str) -> dict:
    errors: list[str] = []
    page = context.new_page()
    page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type in {"error", "warning"} else None)
    page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
    page.goto(base_url, wait_until="networkidle")
    page.get_by_text("SQLite-backed localhost scaffold").wait_for(timeout=5000)
    page.get_by_text("DB reachable").wait_for(timeout=5000)
    page.select_option("#scenarioSelect", "blocked")
    page.get_by_role("button", name="Approve fiat fallback").click()
    page.wait_for_function("() => document.getElementById('operatorActions')?.innerText.includes('fallback_approved')", timeout=5000)
    page.get_by_text("Persisted operator actions").wait_for(timeout=3000)
    evidence = page.evaluate("async () => await fetch('/api/evidence/blocked').then(r => r.json())")
    action_count = len(evidence.get("operator_actions", []))
    audit_count = len(evidence.get("audit_events", []))
    if evidence.get("evidence_type") != "settlement_endpoint_control_tower_db_export":
        raise AssertionError(f"DB_EVIDENCE_TYPE {evidence.get('evidence_type')}")
    if action_count < 1 or audit_count < 7:
        raise AssertionError(f"DB_READBACK_COUNTS actions={action_count} audits={audit_count}")
    if evidence.get("boundary") != "synthetic_data_only_no_external_network_calls_no_proprietary_reference_rows":
        raise AssertionError(f"DB_BOUNDARY {evidence.get('boundary')}")
    target = ARTIFACT_DIR / f"db-{viewport_name}-evidence.json"
    target.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    assert_no_bad_text(page, f"db-{viewport_name}")
    assert_no_horizontal_overflow(page, f"db-{viewport_name}")
    assert_no_console_or_page_errors(errors, f"db-{viewport_name}")
    shot = ARTIFACT_DIR / f"db-{viewport_name}.png"
    page.screenshot(path=str(shot), full_page=True)
    page.close()
    return {"surface": "db", "viewport": viewport_name, "screenshot": str(shot.relative_to(ROOT)), "evidence": str(target.relative_to(ROOT)), "actions": action_count, "audit_events": audit_count}


def main() -> None:
    if not shutil.which("python3"):
        raise SystemExit("python3 missing")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    static_port = free_port()
    db_port = free_port()
    static_proc: subprocess.Popen[str] | None = None
    db_proc: subprocess.Popen[str] | None = None
    results: list[dict] = []
    try:
        static_proc = start_static(static_port)
        db_proc = start_db(db_port)
        static_url = f"http://127.0.0.1:{static_port}/"
        db_url = f"http://127.0.0.1:{db_port}/"
        wait_http(static_url)
        wait_http(static_url + "robots.txt")
        wait_http(static_url + "llms.txt")
        wait_http(db_url + "readyz")
        with sync_playwright() as p:
            for viewport_name, spec in VIEWPORTS.items():
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": spec["width"], "height": spec["height"]},
                    is_mobile=spec["is_mobile"],
                    has_touch=spec["is_mobile"],
                    device_scale_factor=2 if spec["is_mobile"] else 1,
                    accept_downloads=True,
                )
                try:
                    results.append(smoke_static(context, static_url, viewport_name))
                    results.append(smoke_db(context, db_url, viewport_name))
                finally:
                    context.close()
                    browser.close()
    except PlaywrightTimeoutError as exc:
        raise SystemExit(f"BROWSER_TIMEOUT {exc}") from exc
    except PlaywrightError as exc:
        raise SystemExit(f"BROWSER_ERROR {exc}") from exc
    finally:
        stop(db_proc)
        stop(static_proc)

    report = {
        "status": "PASS",
        "static_url": "localhost-only smoke",
        "db_url": "localhost-only smoke",
        "viewports": list(VIEWPORTS.keys()),
        "results": results,
    }
    (ARTIFACT_DIR / "browser-mobile-smoke-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("BROWSER_MOBILE_SMOKE PASS")
    print(f"artifact_dir={ARTIFACT_DIR.relative_to(ROOT)}")
    for row in results:
        print(json.dumps(row, sort_keys=True))


if __name__ == "__main__":
    main()
