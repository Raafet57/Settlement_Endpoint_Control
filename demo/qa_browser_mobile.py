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


def smoke_static(context, base_url: str, viewport_name: str) -> dict:
    errors: list[str] = []
    page = context.new_page()
    page.on("console", lambda msg: errors.append(f"console:{msg.type}:{msg.text}") if msg.type in {"error", "warning"} else None)
    page.on("pageerror", lambda exc: errors.append(f"pageerror:{exc}"))
    page.goto(base_url, wait_until="networkidle")
    page.get_by_role("heading", name="Identity-bound endpoints before value moves.").wait_for(timeout=5000)
    page.locator('[data-screen="profile"]').click()
    page.locator('[data-screen="validation"]').click()
    page.get_by_text("Endpoint pre-validation").wait_for(timeout=3000)
    page.locator('[data-screen="decision"]').click()
    page.get_by_text("Policy route decision").wait_for(timeout=3000)
    page.locator('[data-screen="audit"]').click()
    page.get_by_text("Evidence and audit pack").wait_for(timeout=3000)
    page.select_option("#scenarioSelect", "refreshed")
    evidence_text = page.locator("#evidenceJson").inner_text(timeout=3000)
    if "TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED" not in evidence_text:
        raise AssertionError("STATIC_SCENARIO_SWITCH_FAILED refreshed verdict missing")
    page.select_option("#roleSelect", "risk")
    role_note = page.locator("#roleNote").inner_text(timeout=3000)
    if "Risk reviewer view" not in role_note:
        raise AssertionError("STATIC_ROLE_SWITCH_FAILED risk note missing")
    with page.expect_download(timeout=3000) as download_info:
        page.get_by_role("button", name="Download synthetic evidence JSON").click()
    download = download_info.value
    target = ARTIFACT_DIR / f"static-{viewport_name}-evidence.json"
    download.save_as(target)
    evidence = json.loads(target.read_text(encoding="utf-8"))
    if evidence.get("boundary") != "synthetic_data_only_no_external_network_calls":
        raise AssertionError(f"STATIC_EVIDENCE_BOUNDARY {evidence.get('boundary')}")
    assert_no_bad_text(page, f"static-{viewport_name}")
    assert_no_horizontal_overflow(page, f"static-{viewport_name}")
    assert_no_console_or_page_errors(errors, f"static-{viewport_name}")
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
