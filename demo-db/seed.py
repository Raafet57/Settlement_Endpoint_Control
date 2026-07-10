#!/usr/bin/env python3
"""Create and seed the Stage 2 SQLite demo database.

Synthetic fixture only. No secrets, no external services, no production data.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import migrate
from evaluator import evaluate

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "demo.sqlite"
MANIFEST_PATH = ROOT / "source_manifest.json"
# Single source of truth for the current schema version lives in migrate.py so the
# destructive fresh seed and the nondestructive migration path can never drift.
SCHEMA_VERSION = migrate.CURRENT_SCHEMA_VERSION

LINEAGE_BIC = json.dumps({
    "kind": "source_lineage",
    "mode": "synthetic_record_shape_informed",
    "source_ids": ["iso_20022_concepts", "iban_structure_concepts"],
    "disclosure": "Synthetic institution profile; no external reference row copied or displayed.",
}, sort_keys=True)
LINEAGE_LEI_VLEI = json.dumps({
    "kind": "source_lineage",
    "mode": "synthetic_profile_external_concept",
    "source_ids": ["lei_authority_concepts"],
    "disclosure": "Synthetic LEI/vLEI-style authority profile; no live GLEIF or vLEI verification claimed.",
}, sort_keys=True)
LINEAGE_ENDPOINT = json.dumps({
    "kind": "source_lineage",
    "mode": "synthetic_endpoint_with_structural_fallback",
    "source_ids": ["travel_rule_concepts", "iban_structure_concepts"],
    "disclosure": "Synthetic wallet and fallback account; IBAN/SSI semantics are shape-informed only.",
}, sort_keys=True)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS institutions (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    bic TEXT NOT NULL UNIQUE,
    jurisdiction TEXT NOT NULL,
    reachability TEXT NOT NULL,
    source_lineage TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legal_entities (
    id INTEGER PRIMARY KEY,
    institution_id INTEGER NOT NULL REFERENCES institutions(id),
    name TEXT NOT NULL,
    lei TEXT NOT NULL UNIQUE,
    authority_status TEXT NOT NULL,
    authority_detail TEXT NOT NULL,
    maintainer TEXT NOT NULL,
    source_lineage TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlement_endpoints (
    id INTEGER PRIMARY KEY,
    legal_entity_id INTEGER NOT NULL REFERENCES legal_entities(id),
    wallet_address TEXT NOT NULL,
    custody TEXT NOT NULL,
    allowlist_status TEXT NOT NULL,
    endpoint_owner TEXT NOT NULL,
    endpoint_payload_status TEXT NOT NULL,
    requested_rail TEXT NOT NULL,
    fallback_rail TEXT NOT NULL,
    fallback_currency TEXT NOT NULL,
    fallback_account_mask TEXT NOT NULL,
    fallback_intermediary_bic TEXT NOT NULL,
    uetr TEXT NOT NULL UNIQUE,
    source_lineage TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS route_policies (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS demo_scenarios (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    context_class TEXT NOT NULL,
    context_label TEXT NOT NULL,
    profile_class TEXT NOT NULL,
    profile_label TEXT NOT NULL,
    validation_class TEXT NOT NULL,
    validation_label TEXT NOT NULL,
    decision_class TEXT NOT NULL,
    decision_label TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL REFERENCES settlement_endpoints(id),
    authority_display TEXT NOT NULL,
    allowlist_display TEXT NOT NULL,
    custody_display TEXT NOT NULL,
    owner_display TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_checks (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES demo_scenarios(id),
    policy_id INTEGER NOT NULL REFERENCES route_policies(id),
    display_order INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('good','warn','bad')),
    name TEXT NOT NULL,
    detail TEXT NOT NULL,
    UNIQUE(scenario_id, policy_id)
);

CREATE TABLE IF NOT EXISTS route_decisions (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL UNIQUE REFERENCES demo_scenarios(id),
    verdict TEXT NOT NULL,
    token_class TEXT NOT NULL,
    fiat_class TEXT NOT NULL,
    token_text TEXT NOT NULL,
    fiat_text TEXT NOT NULL,
    repair_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES demo_scenarios(id),
    display_order INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, display_order)
);

CREATE TABLE IF NOT EXISTS operator_actions (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES demo_scenarios(id),
    action_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_manifest (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    classification TEXT NOT NULL,
    data_row_estimate INTEGER NOT NULL,
    sha256_prefix TEXT NOT NULL,
    role TEXT NOT NULL
);
"""

POLICIES = [
    (1, "institution_shape", "BIC and institution shape", "Institution and reachability profile is present."),
    (2, "legal_entity_profile", "LEI and counterparty profile", "Legal entity profile is present for the beneficiary."),
    (3, "authority_evidence", "vLEI-style authority evidence", "Authority chain evidence is current enough for token use."),
    (4, "allowlist_freshness", "Wallet allowlist freshness", "Endpoint allowlist is current for this rail."),
    (5, "endpoint_payload", "Endpoint-control payload", "Endpoint-control payload is complete."),
    (6, "fiat_fallback", "Fiat SSI fallback", "Fallback route is present and traceable."),
]

SCENARIOS = {
    "blocked": {
        "row": (1, "blocked", "Blocked wallet endpoint", "blocked_wallet_endpoint", "bad", "fallback selected", "warn", "pre-release", "bad", "blocked", "bad", "fallback selected", 1, "vLEI-style evidence expiring soon", "Stale, refresh required", "Approved custodian, endpoint evidence incomplete", "Treasury ops queue"),
        "rule_input": {
            "institution_present": True,
            "institution_reachable": True,
            "legal_entity_present": True,
            "authority_status": "expiring_soon",
            "allowlist_status": "stale",
            "payload_status": "incomplete",
            "fallback_rail": "Fiat SSI route",
            "fallback_currency": "EUR",
            "fallback_account_mask": "DE•• •••• •••• 4400",
            "fallback_intermediary_bic": "INTERDEFFXXX",
        },
        "audit": [
            ("logged", "Profile loaded", "BIC, LEI/vLEI-style authority, wallet endpoint and fallback SSI loaded."),
            ("logged", "Pre-validation started", "Tokenized deposit route checked before release."),
            ("exception", "Wallet allowlist failed", "Allowlist evidence is stale for requested rail."),
            ("exception", "Endpoint-control failed", "Required synthetic endpoint-control field is incomplete."),
            ("decision", "Policy decision", "Token route blocked. Fiat SSI fallback selected."),
            ("action", "Repair task opened", "Treasury operations must refresh endpoint evidence."),
        ],
    },
    "refreshed": {
        "row": (2, "refreshed", "Refreshed endpoint approved", "refreshed_endpoint_approved", "good", "token route eligible", "good", "ready", "good", "eligible", "good", "token route approved", 1, "vLEI-style evidence refreshed", "Current for counterparty and rail", "Approved custodian, current evidence attached", "Treasury ops approved"),
        "rule_input": {
            "institution_present": True,
            "institution_reachable": True,
            "legal_entity_present": True,
            "authority_status": "current",
            "allowlist_status": "current",
            "payload_status": "complete",
            "fallback_rail": "Fiat SSI route",
            "fallback_currency": "EUR",
            "fallback_account_mask": "DE•• •••• •••• 4400",
            "fallback_intermediary_bic": "INTERDEFFXXX",
        },
        "audit": [
            ("logged", "Profile loaded", "BIC, LEI/vLEI-style authority, wallet endpoint and fallback SSI loaded."),
            ("logged", "Pre-validation started", "Tokenized deposit route checked before release."),
            ("logged", "Endpoint evidence passed", "Wallet allowlist and authority evidence are current."),
            ("logged", "Endpoint-control passed", "Required synthetic endpoint payload is complete."),
            ("decision", "Policy decision", "Tokenized deposit route approved."),
            ("logged", "Fallback retained", "Fiat SSI remains visible as contingency route."),
        ],
    },
    "authority": {
        "row": (3, "authority", "Authority evidence expired", "authority_expired_manual_hold", "warn", "approval hold", "warn", "authority expired", "warn", "hold", "warn", "manual approval hold", 1, "vLEI-style evidence expired in fixture", "Current but authority chain stale", "Approved custodian, approval hold active", "Risk reviewer queue"),
        "rule_input": {
            "institution_present": True,
            "institution_reachable": True,
            "legal_entity_present": True,
            "authority_status": "expired",
            "allowlist_status": "current",
            "payload_status": "complete",
            "fallback_rail": "Fiat SSI route",
            "fallback_currency": "EUR",
            "fallback_account_mask": "DE•• •••• •••• 4400",
            "fallback_intermediary_bic": "INTERDEFFXXX",
        },
        "audit": [
            ("logged", "Profile loaded", "BIC, LEI/vLEI-style authority, wallet endpoint and fallback SSI loaded."),
            ("logged", "Pre-validation started", "Authority chain and endpoint controls checked."),
            ("exception", "Authority evidence failed", "Synthetic authority evidence is expired."),
            ("logged", "Allowlist passed", "Wallet endpoint allowlist is current."),
            ("decision", "Policy decision", "Manual approval hold. Fiat fallback available."),
            ("action", "Risk task opened", "Risk reviewer must renew or override using approved workflow."),
        ],
    },
}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def load_source_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def seed() -> dict[str, int | str]:
    if DB_PATH.exists():
        DB_PATH.unlink()
    with connect() as con:
        con.executescript(SCHEMA)
        con.executescript(migrate.SCHEMA_MIGRATIONS_DDL)
        con.executescript(migrate.ENDPOINT_PROFILES_DDL)
        con.executescript(migrate.PROFILE_DECISIONS_DDL)
        con.executescript(migrate.REPAIR_TASKS_DDL)
        con.executescript(migrate.REPAIR_TASKS_OPEN_INDEX_DDL)
        con.executescript(migrate.REPAIR_EVENTS_DDL)
        with con:
            for table in ["repair_events", "repair_tasks", "profile_decisions", "endpoint_profiles", "operator_actions", "audit_events", "route_decisions", "policy_checks", "demo_scenarios", "route_policies", "settlement_endpoints", "legal_entities", "institutions", "source_manifest"]:
                con.execute(f"DELETE FROM {table}")

            manifest = load_source_manifest()
            con.executemany(
                "INSERT INTO source_manifest(source_id, name, classification, data_row_estimate, sha256_prefix, role) VALUES (?,?,?,?,?,?)",
                [
                    (source["source_id"], source["name"], source["classification"], source["data_row_estimate"], source["sha256_prefix"], source["role"])
                    for source in manifest["sources"]
                ],
            )

            con.executemany(
                "INSERT INTO institutions(id, role, name, bic, jurisdiction, reachability, source_lineage) VALUES (?,?,?,?,?,?,?)",
                [
                    (1, "debtor_agent", "Atlas Bank Singapore", "ATLASGSGXXX", "SG synthetic profile", "Debtor institution for demo payment.", LINEAGE_BIC),
                    (2, "creditor_agent", "Meridian Bank Europe", "MERIDEFFXXX", "EU synthetic profile", "Synthetic institution evidence and fallback SSI holder.", LINEAGE_BIC),
                ],
            )
            con.execute(
                "INSERT INTO legal_entities(id, institution_id, name, lei, authority_status, authority_detail, maintainer, source_lineage) VALUES (?,?,?,?,?,?,?,?)",
                (1, 2, "Northstar Components GmbH", "549300SYNTHETIC01", "scenario_driven", "vLEI-style evidence represented by selected scenario.", "Four-eyes treasury operations", LINEAGE_LEI_VLEI),
            )
            con.execute(
                """INSERT INTO settlement_endpoints(
                    id, legal_entity_id, wallet_address, custody, allowlist_status, endpoint_owner, endpoint_payload_status,
                    requested_rail, fallback_rail, fallback_currency, fallback_account_mask, fallback_intermediary_bic, uetr, source_lineage
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (1, 1, "0x71...9F4A", "Approved custodian", "scenario_driven", "scenario_driven", "scenario_driven", "Tokenized deposit", "Fiat SSI route", "EUR", "DE•• •••• •••• 4400", "INTERDEFFXXX", "SYN-2026-000184", LINEAGE_ENDPOINT),
            )
            con.executemany("INSERT INTO route_policies(id, code, name, description) VALUES (?,?,?,?)", POLICIES)

            check_id = 1
            audit_id = 1
            fixed_time = "2026-06-05T09:00:00Z"
            for scenario in SCENARIOS.values():
                con.execute(
                    """INSERT INTO demo_scenarios(
                        id, slug, title, state, context_class, context_label, profile_class, profile_label,
                        validation_class, validation_label, decision_class, decision_label, endpoint_id,
                        authority_display, allowlist_display, custody_display, owner_display
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    scenario["row"],
                )
                scenario_id = scenario["row"][0]
                evaluation = evaluate(scenario["rule_input"])
                for order, check in enumerate(evaluation["checks"], start=1):
                    con.execute(
                        "INSERT INTO policy_checks(id, scenario_id, policy_id, display_order, status, name, detail) VALUES (?,?,?,?,?,?,?)",
                        (check_id, scenario_id, order, order, check["status"], check["name"], check["detail"]),
                    )
                    check_id += 1
                decision = evaluation["decision"]
                con.execute(
                    "INSERT INTO route_decisions(id, scenario_id, verdict, token_class, fiat_class, token_text, fiat_text, repair_text, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (scenario_id, scenario_id, decision["verdict"], decision["token_class"], decision["fiat_class"], decision["token_text"], decision["fiat_text"], decision["repair_text"], fixed_time),
                )
                for order, (event_type, title, detail) in enumerate(scenario["audit"], start=1):
                    con.execute(
                        "INSERT INTO audit_events(id, scenario_id, display_order, event_type, title, detail, created_at) VALUES (?,?,?,?,?,?,?)",
                        (audit_id, scenario_id, order, event_type, title, detail, fixed_time),
                    )
                    audit_id += 1

            con.execute("INSERT OR REPLACE INTO seed_meta(key, value) VALUES (?,?)", ("schema_version", str(SCHEMA_VERSION)))
            con.execute("INSERT OR REPLACE INTO seed_meta(key, value) VALUES (?,?)", ("fixture_boundary", "synthetic_data_only"))
            con.execute("INSERT OR REPLACE INTO seed_meta(key, value) VALUES (?,?)", ("source_file_count", str(manifest["lineage_summary"]["source_file_count"])))
            con.execute("INSERT OR REPLACE INTO seed_meta(key, value) VALUES (?,?)", ("source_row_estimate", str(manifest["lineage_summary"]["source_row_estimate"])))
            con.execute("INSERT OR REPLACE INTO seed_meta(key, value) VALUES (?,?)", ("source_manifest_boundary", manifest["boundary"]))

            # Foundation metadata: a freshly seeded DB is born at the current
            # version with the migration ledger already populated. The ledger
            # applied_at uses the same fixed fixture time so fresh seed stays
            # byte-deterministic; PRAGMA user_version is the authoritative marker.
            for version, name in migrate.current_ledger():
                con.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                    (version, name, fixed_time),
                )
            con.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

            # A freshly seeded DB is born at the current schema with the registry
            # backfilled to match the 4->5 migration: one active profile per
            # settlement endpoint, under the single server-owned synthetic tenant.
            migrate.backfill_active_profiles(con, migrate.SYNTHETIC_TENANT_ID, fixed_time)

        counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in [
            "institutions", "legal_entities", "settlement_endpoints", "route_policies", "demo_scenarios", "policy_checks", "route_decisions", "audit_events", "operator_actions", "source_manifest", "schema_migrations", "endpoint_profiles", "profile_decisions", "repair_tasks", "repair_events"
        ]}
        counts["database"] = str(DB_PATH)
        return counts


def main() -> None:
    counts = seed()
    print("SEED PASS")
    for key in sorted(counts):
        print(f"{key}={counts[key]}")


if __name__ == "__main__":
    main()
