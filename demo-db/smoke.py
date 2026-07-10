#!/usr/bin/env python3
"""Smoke test for the Stage 2 DB-backed demo scaffold."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import migrate

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
    "schema_migrations": 3,
    "endpoint_profiles": 1,
    "profile_decisions": 0,
    "repair_tasks": 0,
    "repair_events": 0,
}
EXPECTED_SLUGS = {"blocked", "refreshed", "authority"}
EXPECTED_VERDICTS = {
    "blocked": "TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED",
    "refreshed": "TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED",
    "authority": "AUTHORITY_EXPIRED_MANUAL_HOLD",
}
APPROVED = EXPECTED_VERDICTS["refreshed"]
BLOCKED = EXPECTED_VERDICTS["blocked"]
# The backfilled active profile 1 wraps the shared scenario endpoint, whose
# constituent evidence fields are scenario_driven placeholders; its intrinsic
# advisory verdict is a manual hold (blocked from automated release), which the
# SEC-P30 repair loop supersedes with an ALLOW from refreshed evidence.
BASELINE_HOLD = "INSUFFICIENT_INPUT_MANUAL_HOLD"


def profile_body(tag: str, *, authority: str = "current", allowlist: str = "current", payload: str = "complete") -> dict:
    """A distinct, fully-formed synthetic endpoint-profile create body."""
    return {
        "institution": {"name": f"Smoke Institution {tag}", "bic": f"SMOKEBIC{tag}", "jurisdiction": "EU synthetic profile"},
        "legal_entity": {"name": f"Smoke Entity {tag}", "lei": f"SMOKELEI{tag}", "authority_status": authority},
        "endpoint": {
            "wallet_address": f"0xSMOKE{tag}", "custody": "Approved custodian", "allowlist_status": allowlist,
            "endpoint_owner": "Treasury ops queue", "endpoint_payload_status": payload,
            "requested_rail": "Tokenized deposit", "uetr": f"SMOKE-UETR-{tag}",
        },
        "fallback": {
            "fallback_rail": "Fiat SSI route", "fallback_currency": "EUR",
            "fallback_account_mask": "DE•• •••• •••• 4400", "fallback_intermediary_bic": "INTERDEFFXXX",
        },
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


def put_json(base: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=4) as response:  # noqa: S310 - localhost smoke only
        return json.loads(response.read().decode("utf-8"))


def send_status(base: str, path: str, method: str, payload: dict | None = None) -> int:
    """Send a request and return only the HTTP status (deterministic, bounded)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=4) as response:  # noqa: S310 - localhost smoke only
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


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
        if ready.get("endpoint_profile_count") != 1:
            raise SystemExit(f"READYZ_PROFILE_BAD {ready}")
        if ready.get("schema_version") != 6 or ready.get("source_file_count") != 4 or ready.get("source_row_estimate") != 0:
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

        # --- Endpoint-profile registry lifecycle (SEC-P20) ---
        backfilled = get_json(base, "/api/endpoint-profiles")["endpoint_profiles"]
        if len(backfilled) != 1 or backfilled[0]["lifecycle_state"] != "active":
            raise SystemExit(f"PROFILE_BACKFILL_BAD {backfilled}")

        # Create a draft; evaluation is computed live from persisted fields.
        approved = post_json(base, "/api/endpoint-profiles", profile_body("A"))
        profile_id = approved["profile"]["id"]
        if approved["profile"]["lifecycle_state"] != "draft" or approved["evaluation"]["decision"]["verdict"] != APPROVED:
            raise SystemExit(f"PROFILE_CREATE_BAD {approved.get('profile')}")

        # Draft update independently changes the verdict (no shared fixture state).
        updated = put_json(base, f"/api/endpoint-profiles/{profile_id}", profile_body("A", allowlist="stale"))
        if updated["evaluation"]["decision"]["verdict"] != BLOCKED:
            raise SystemExit(f"PROFILE_UPDATE_EVAL_BAD {updated['evaluation']['decision']['verdict']}")
        put_json(base, f"/api/endpoint-profiles/{profile_id}", profile_body("A"))  # restore approved shape

        activated = post_json(base, f"/api/endpoint-profiles/{profile_id}/activation", {})
        if activated["profile"]["lifecycle_state"] != "active":
            raise SystemExit(f"PROFILE_ACTIVATE_BAD {activated['profile']}")
        # An active profile is immutable through the API.
        if send_status(base, f"/api/endpoint-profiles/{profile_id}", "PUT", profile_body("A")) != 409:
            raise SystemExit("PROFILE_ACTIVE_MUTABLE")

        # Supersede atomically with a distinct draft replacement.
        replacement = post_json(base, "/api/endpoint-profiles", profile_body("B", allowlist="stale"))
        replacement_id = replacement["profile"]["id"]
        if replacement["evaluation"]["decision"]["verdict"] != BLOCKED:
            raise SystemExit(f"PROFILE_REPLACEMENT_EVAL_BAD {replacement['evaluation']['decision']['verdict']}")
        superseded = post_json(base, f"/api/endpoint-profiles/{profile_id}/supersession", {"replacement_id": replacement_id})
        if superseded["profile"]["profile"]["lifecycle_state"] != "superseded" or superseded["replacement"]["profile"]["lifecycle_state"] != "active":
            raise SystemExit(f"PROFILE_SUPERSEDE_BAD {superseded}")

        # The old profile is preserved, readable, and links to its replacement.
        old = get_json(base, f"/api/endpoint-profiles/{profile_id}")
        if old["profile"]["lifecycle_state"] != "superseded" or old["profile"]["superseded_by"] != replacement_id:
            raise SystemExit(f"PROFILE_HISTORY_BAD {old['profile']}")
        # There is no delete route.
        if send_status(base, f"/api/endpoint-profiles/{profile_id}", "DELETE") not in (404, 405):
            raise SystemExit("PROFILE_DELETE_ROUTE_PRESENT")

        # --- SEC-P30 repair -> revalidation on the existing blocked active profile 1 ---
        # Profile 1 is the backfilled active profile wrapping the shared scenario
        # endpoint; its intrinsic advisory verdict is a manual hold. Carry it through
        # open -> evidence refresh -> revalidation and prove the advisory overlay
        # supersedes the baseline without mutating the immutable active profile.
        before_repair = get_json(base, "/api/endpoint-profiles/1")
        if before_repair["profile"]["lifecycle_state"] != "active":
            raise SystemExit(f"REPAIR_PROFILE_NOT_ACTIVE {before_repair['profile']}")

        opened = post_json(base, "/api/endpoint-profiles/1/repair", {"actor": "ops_analyst"})
        baseline = opened["decision"]
        if opened["repair_task"]["state"] != "open":
            raise SystemExit(f"REPAIR_OPEN_STATE_BAD {opened['repair_task']}")
        if baseline["version"] != 1 or baseline["origin"] != "baseline" or baseline["previous_decision_id"] is not None:
            raise SystemExit(f"REPAIR_BASELINE_SHAPE_BAD {baseline}")
        if baseline["verdict"] != BASELINE_HOLD or baseline["verdict"] == APPROVED:
            raise SystemExit(f"REPAIR_BASELINE_NOT_BLOCKED {baseline['verdict']}")

        refreshed = post_json(base, "/api/endpoint-profiles/1/repair/evidence",
                              {"authority_status": "current", "allowlist_status": "current", "endpoint_payload_status": "complete"})
        if refreshed["repair_task"]["state"] != "evidence_refreshed":
            raise SystemExit(f"REPAIR_EVIDENCE_STATE_BAD {refreshed['repair_task']}")

        revalidated = post_json(base, "/api/endpoint-profiles/1/repair/revalidation", {})
        superseding = revalidated["decision"]
        if superseding["version"] != 2 or superseding["origin"] != "revalidation":
            raise SystemExit(f"REPAIR_REVAL_SHAPE_BAD {superseding}")
        if superseding["verdict"] != APPROVED:
            raise SystemExit(f"REPAIR_REVAL_NOT_ALLOW {superseding['verdict']}")
        if superseding["previous_decision_id"] != baseline["id"]:
            raise SystemExit(f"REPAIR_REVAL_LINK_BAD {superseding}")
        if revalidated["repair_task"]["state"] != "resolved":
            raise SystemExit(f"REPAIR_TASK_NOT_RESOLVED {revalidated['repair_task']}")

        after_repair = get_json(base, "/api/endpoint-profiles/1")
        decisions = after_repair["decisions"]
        if [d["version"] for d in decisions] != [1, 2]:
            raise SystemExit(f"REPAIR_DECISION_VERSIONS_BAD {[d['version'] for d in decisions]}")
        # The prior decision remains queryable with its immutable evidence snapshot.
        if decisions[0]["verdict"] != BASELINE_HOLD or not decisions[0].get("evidence"):
            raise SystemExit(f"REPAIR_PRIOR_DECISION_BAD {decisions[0]}")
        if decisions[1]["verdict"] != APPROVED:
            raise SystemExit(f"REPAIR_LATEST_DECISION_BAD {decisions[1]}")
        # Exact ordered append-only event trail.
        if [e["event_type"] for e in after_repair["repair_events"]] != ["repair_opened", "evidence_refreshed", "revalidated"]:
            raise SystemExit(f"REPAIR_EVENT_TRAIL_BAD {[e['event_type'] for e in after_repair['repair_events']]}")
        # The immutable active profile is unchanged: lifecycle, constituent source
        # fields, and intrinsic evaluation all identical to before the repair.
        if after_repair["profile"]["lifecycle_state"] != "active":
            raise SystemExit(f"REPAIR_PROFILE_MUTATED_STATE {after_repair['profile']}")
        if after_repair["endpoint"] != before_repair["endpoint"]:
            raise SystemExit("REPAIR_PROFILE_ENDPOINT_MUTATED")
        if after_repair["evaluation"]["decision"]["verdict"] != before_repair["evaluation"]["decision"]["verdict"]:
            raise SystemExit("REPAIR_PROFILE_EVAL_MUTATED")

        # SEC-P30: the read surfaces promote the latest advisory verdict (ALLOW) while
        # the intrinsic evaluation stays separately available and unchanged (a hold).
        if after_repair.get("latest_decision", {}).get("verdict") != APPROVED or after_repair.get("verdict") != APPROVED:
            raise SystemExit(f"REPAIR_ADVISORY_NOT_PROMOTED {after_repair.get('verdict')}")
        if after_repair["evaluation"]["decision"]["verdict"] != BASELINE_HOLD:
            raise SystemExit(f"REPAIR_INTRINSIC_NOT_SEPARATE {after_repair['evaluation']['decision']['verdict']}")
        collection_after = {p["id"]: p for p in get_json(base, "/api/endpoint-profiles")["endpoint_profiles"]}
        if collection_after[1]["verdict"] != APPROVED:
            raise SystemExit(f"COLLECTION_VERDICT_NOT_PROMOTED {collection_after[1]['verdict']}")

        # The three shipped scenarios still reproduce byte-for-byte after profile
        # writes and the repair overlay.
        for slug, verdict in EXPECTED_VERDICTS.items():
            after = get_json(base, f"/api/scenarios/{slug}")
            if after["decision"]["verdict"] != verdict or after["payment_context"]["settlement_endpoint"]["id"] != 1:
                raise SystemExit(f"SCENARIO_DRIFT_AFTER_PROFILES {slug} {after['decision']['verdict']}")
        profile_summary = {"created": profile_id, "replacement": replacement_id, "superseded_link": old["profile"]["superseded_by"]}
        repair_summary = {
            "profile": 1, "baseline_verdict": baseline["verdict"], "revalidated_verdict": superseding["verdict"],
            "versions": [d["version"] for d in decisions], "task_state": revalidated["repair_task"]["state"],
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=4)

    # A completed shipped-profile repair leaves a valid v6 database: migrate.py
    # accepts it as already_current. The backfilled profile's baseline evidence is
    # the intrinsic (scenario_driven) fields, which origin-aware validation permits,
    # and every stored decision matches the deterministic evaluator output.
    migrate_result = migrate.migrate(DB)
    if migrate_result.get("status") != "already_current":
        raise SystemExit(f"MIGRATE_NOT_ALREADY_CURRENT {migrate_result}")

    print("SMOKE PASS")
    print(f"database={DB}")
    print(f"seed_counts={json.dumps(EXPECTED_TABLE_COUNTS, sort_keys=True)}")
    print(f"readyz={json.dumps(ready, sort_keys=True)}")
    print(f"scenarios={','.join(sorted(EXPECTED_SLUGS))}")
    print(f"audit_counts={json.dumps(audit_counts, sort_keys=True)}")
    print(f"source_manifest={json.dumps(source_manifest['lineage_summary'], sort_keys=True)}")
    print(f"endpoint_profiles={json.dumps(profile_summary, sort_keys=True)}")
    print(f"repair_revalidation={json.dumps(repair_summary, sort_keys=True)}")
    print(f"repair_migrate={migrate_result['status']}")


if __name__ == "__main__":
    main()
