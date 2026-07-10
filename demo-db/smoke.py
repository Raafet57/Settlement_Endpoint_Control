#!/usr/bin/env python3
"""Smoke test for the Stage 2 DB-backed demo scaffold."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "seed.py"
APP = ROOT / "app.py"
DB = ROOT / "data" / "demo.sqlite"
EXPECTED_TABLE_COUNTS = {
    "institutions": 2,
    "legal_entities": 1,
    "settlement_endpoints": 1,
    "route_policies": 6,
    "demo_scenarios": 3,
    "policy_checks": 18,
    "route_decisions": 3,
    "audit_events": 18,
    "operator_actions": 0,
    "source_manifest": 4,
    "schema_migrations": 1,
}
EXPECTED_SLUGS = {"blocked", "refreshed", "authority"}
EXPECTED_VERDICTS = {
    "blocked": "TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED",
    "refreshed": "TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED",
    "authority": "AUTHORITY_EXPIRED_MANUAL_HOLD",
}


def run_seed() -> dict[str, str]:
    proc = subprocess.run([sys.executable, str(SEED)], cwd=str(ROOT.parent), check=True, text=True, capture_output=True)
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=4) as response:  # noqa: S310 - localhost smoke only
        return json.loads(response.read().decode("utf-8"))


def post_json(base: str, path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=4) as response:  # noqa: S310 - localhost smoke only
        return json.loads(response.read().decode("utf-8"))


def wait_ready(base: str) -> dict:
    last_error: Exception | None = None
    for _ in range(40):
        try:
            return get_json(base, "/readyz")
        except Exception as exc:  # noqa: BLE001 - surface final error after retries
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


def main() -> None:
    first = run_seed()
    second = run_seed()
    if first != second:
        raise SystemExit(f"SEED_NOT_IDEMPOTENT first={first} second={second}")
    for table, expected in EXPECTED_TABLE_COUNTS.items():
        actual = int(second.get(table, -1))
        if actual != expected:
            raise SystemExit(f"COUNT_MISMATCH {table} expected={expected} actual={actual}")
    if not DB.exists():
        raise SystemExit("DATABASE_MISSING")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(APP), "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT.parent),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = wait_ready(base)
        if ready.get("status") != "ok" or ready.get("database") != "reachable" or ready.get("scenario_count") != 3:
            raise SystemExit(f"READYZ_BAD {ready}")
        if ready.get("schema_version") != 4 or ready.get("source_file_count") != 4 or ready.get("source_row_estimate") != 0:
            raise SystemExit(f"READYZ_SOURCE_BAD {ready}")

        with urllib.request.urlopen(base + "/", timeout=4) as response:  # noqa: S310 - localhost smoke only
            index_html = response.read().decode("utf-8")
        if "Scenario data loaded from SQLite" not in index_html or "reference manifest" not in index_html or "fetch(path" not in index_html:
            raise SystemExit("INDEX_NOT_DB_API_UI")

        source_manifest = get_json(base, "/api/source-manifest")
        if source_manifest["lineage_summary"]["source_file_count"] != 4 or source_manifest["lineage_summary"]["source_row_estimate"] != 0:
            raise SystemExit(f"SOURCE_MANIFEST_BAD {source_manifest.get('lineage_summary')}")

        scenario_index = get_json(base, "/api/scenarios")
        slugs = {item["slug"] for item in scenario_index["scenarios"]}
        if slugs != EXPECTED_SLUGS:
            raise SystemExit(f"SCENARIO_SLUGS_BAD {slugs}")

        for slug, verdict in EXPECTED_VERDICTS.items():
            payload = get_json(base, f"/api/scenarios/{slug}")
            if payload["decision"]["verdict"] != verdict:
                raise SystemExit(f"VERDICT_BAD {slug} {payload['decision']['verdict']}")
            if len(payload["checks"]) != 6:
                raise SystemExit(f"CHECK_COUNT_BAD {slug}")
            if len(payload["audit_events"]) != 6:
                raise SystemExit(f"AUDIT_COUNT_BAD {slug}")
            if payload["boundary"] != "synthetic_data_only_no_external_network_calls":
                raise SystemExit(f"BOUNDARY_BAD {slug}")
            if payload["source_manifest_summary"]["source_file_count"] != 4:
                raise SystemExit(f"SOURCE_SUMMARY_BAD {slug}")
            if not payload["payment_context"]["beneficiary_institution"].get("source_lineage"):
                raise SystemExit(f"SOURCE_LINEAGE_MISSING {slug}")

        audit_counts = get_json(base, "/api/audit/counts")["audit_counts"]
        if audit_counts != {"blocked": 6, "refreshed": 6, "authority": 6}:
            raise SystemExit(f"AUDIT_COUNTS_BAD {audit_counts}")

        action = post_json(base, "/api/scenarios/blocked/actions", {"action_type": "open_repair_task", "actor": "ops_analyst"})
        if action.get("status") != "action_recorded" or action.get("action", {}).get("scenario_slug") != "blocked":
            raise SystemExit(f"ACTION_RESPONSE_BAD {action}")
        action_id = action["action"]["id"]
        refreshed_blocked = get_json(base, "/api/scenarios/blocked")
        if len(refreshed_blocked.get("operator_actions", [])) != 1:
            raise SystemExit(f"OPERATOR_ACTION_NOT_PERSISTED {refreshed_blocked.get('operator_actions')}")
        if refreshed_blocked["operator_actions"][0]["id"] != action_id:
            raise SystemExit(f"OPERATOR_ACTION_ID_BAD {refreshed_blocked['operator_actions']}")
        if len(refreshed_blocked["audit_events"]) != 7:
            raise SystemExit(f"ACTION_AUDIT_NOT_APPENDED {len(refreshed_blocked['audit_events'])}")
        evidence = get_json(base, "/api/evidence/blocked")
        if evidence.get("evidence_type") != "settlement_endpoint_control_tower_db_export":
            raise SystemExit(f"EVIDENCE_TYPE_BAD {evidence.get('evidence_type')}")
        if evidence.get("scenario", {}).get("slug") != "blocked" or len(evidence.get("operator_actions", [])) != 1:
            raise SystemExit("EVIDENCE_EXPORT_MISSING_ACTION")
        if evidence.get("boundary") != "synthetic_data_only_no_external_network_calls_no_proprietary_reference_rows":
            raise SystemExit(f"EVIDENCE_BOUNDARY_BAD {evidence.get('boundary')}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=4)

    print("SMOKE PASS")
    print(f"database={DB}")
    print(f"seed_counts={json.dumps(EXPECTED_TABLE_COUNTS, sort_keys=True)}")
    print(f"readyz={json.dumps(ready, sort_keys=True)}")
    print(f"scenarios={','.join(sorted(EXPECTED_SLUGS))}")
    print(f"audit_counts={json.dumps(audit_counts, sort_keys=True)}")
    print(f"source_manifest={json.dumps(source_manifest['lineage_summary'], sort_keys=True)}")


if __name__ == "__main__":
    main()
