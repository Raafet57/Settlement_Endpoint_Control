#!/usr/bin/env python3
"""Localhost-only Stage 2 demo API for the Settlement Endpoint Control Tower."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "demo.sqlite"
INDEX = ROOT / "index.html"
MANIFEST_PATH = ROOT / "source_manifest.json"


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def row_dict(row: sqlite3.Row | None) -> dict:
    if not row:
        return {}
    data = dict(row)
    if isinstance(data.get("source_lineage"), str):
        data["source_lineage"] = json.loads(data["source_lineage"])
    return data


def load_source_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def source_summary() -> dict:
    manifest = load_source_manifest()
    return manifest["lineage_summary"] | {"boundary": manifest["boundary"]}


def health() -> dict:
    with get_db() as con:
        con.execute("SELECT 1").fetchone()
        schema_version = con.execute("SELECT value FROM seed_meta WHERE key = 'schema_version'").fetchone()
        source_file_count = con.execute("SELECT value FROM seed_meta WHERE key = 'source_file_count'").fetchone()
        source_row_estimate = con.execute("SELECT value FROM seed_meta WHERE key = 'source_row_estimate'").fetchone()
        return {
            "status": "ok",
            "database": "reachable",
            "schema_version": int(schema_version[0]) if schema_version else None,
            "scenario_count": con.execute("SELECT COUNT(*) FROM demo_scenarios").fetchone()[0],
            "source_file_count": int(source_file_count[0]) if source_file_count else 0,
            "source_row_estimate": int(source_row_estimate[0]) if source_row_estimate else 0,
        }


def scenario_list() -> list[dict]:
    with get_db() as con:
        rows = con.execute(
            """SELECT slug, title, state, context_class, context_label, validation_label, decision_label
               FROM demo_scenarios ORDER BY id"""
        ).fetchall()
        return [dict(row) for row in rows]


def audit_counts() -> dict:
    with get_db() as con:
        rows = con.execute(
            """SELECT s.slug, COUNT(e.id) AS audit_events
               FROM demo_scenarios s
               LEFT JOIN audit_events e ON e.scenario_id = s.id
               GROUP BY s.id
               ORDER BY s.id"""
        ).fetchall()
        return {row["slug"]: row["audit_events"] for row in rows}


def load_scenario(slug: str) -> dict | None:
    with get_db() as con:
        scenario = con.execute("SELECT * FROM demo_scenarios WHERE slug = ?", (slug,)).fetchone()
        if not scenario:
            return None
        scenario_id = scenario["id"]
        endpoint = con.execute("SELECT * FROM settlement_endpoints WHERE id = ?", (scenario["endpoint_id"],)).fetchone()
        entity = con.execute("SELECT * FROM legal_entities WHERE id = ?", (endpoint["legal_entity_id"],)).fetchone()
        creditor = con.execute("SELECT * FROM institutions WHERE id = ?", (entity["institution_id"],)).fetchone()
        debtor = con.execute("SELECT * FROM institutions WHERE role = 'debtor_agent' ORDER BY id LIMIT 1").fetchone()
        checks = con.execute(
            """SELECT c.display_order, c.status, c.name, c.detail, p.code AS policy_code
               FROM policy_checks c
               JOIN route_policies p ON p.id = c.policy_id
               WHERE c.scenario_id = ?
               ORDER BY c.display_order""",
            (scenario_id,),
        ).fetchall()
        decision = con.execute("SELECT * FROM route_decisions WHERE scenario_id = ?", (scenario_id,)).fetchone()
        audit = con.execute(
            "SELECT display_order, event_type, title, detail, created_at FROM audit_events WHERE scenario_id = ? ORDER BY display_order",
            (scenario_id,),
        ).fetchall()
        operator_actions = con.execute(
            "SELECT id, action_type, actor, status, detail, created_at FROM operator_actions WHERE scenario_id = ? ORDER BY id",
            (scenario_id,),
        ).fetchall()
        return {
            "scenario": row_dict(scenario),
            "payment_context": {
                "debtor_bank": row_dict(debtor),
                "beneficiary_institution": row_dict(creditor),
                "beneficiary_entity": row_dict(entity),
                "settlement_endpoint": row_dict(endpoint),
            },
            "checks": [dict(row) for row in checks],
            "decision": row_dict(decision),
            "operator_actions": [dict(row) for row in operator_actions],
            "audit_events": [dict(row) for row in audit],
            "source_manifest_summary": source_summary(),
            "boundary": "synthetic_data_only_no_external_network_calls",
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def action_detail(action_type: str) -> tuple[str, str]:
    details = {
        "open_repair_task": ("repair_task_opened", "Operator opened a persisted repair task for endpoint evidence refresh."),
        "approve_fallback": ("fallback_approved", "Operator approved use of the fiat SSI fallback while endpoint evidence is repaired."),
        "hold_payment": ("payment_held", "Operator placed the payment on hold pending authority or endpoint evidence repair."),
    }
    return details.get(action_type, ("recorded", "Operator recorded a synthetic workflow action for this scenario."))


def record_operator_action(slug: str, payload: dict) -> dict | None:
    action_type = str(payload.get("action_type") or "open_repair_task")[:80]
    actor = str(payload.get("actor") or "ops_analyst")[:80]
    status, detail = action_detail(action_type)
    created_at = utc_now()
    with get_db() as con:
        scenario = con.execute("SELECT id, slug FROM demo_scenarios WHERE slug = ?", (slug,)).fetchone()
        if not scenario:
            return None
        scenario_id = scenario["id"]
        with con:
            cur = con.execute(
                "INSERT INTO operator_actions(scenario_id, action_type, actor, status, detail, created_at) VALUES (?,?,?,?,?,?)",
                (scenario_id, action_type, actor, status, detail, created_at),
            )
            next_order = con.execute("SELECT COALESCE(MAX(display_order), 0) + 1 FROM audit_events WHERE scenario_id = ?", (scenario_id,)).fetchone()[0]
            con.execute(
                """INSERT INTO audit_events(scenario_id, display_order, event_type, title, detail, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (scenario_id, next_order, "operator_action", "Persisted operator action", detail, created_at),
            )
        return {
            "id": cur.lastrowid,
            "scenario_slug": scenario["slug"],
            "action_type": action_type,
            "actor": actor,
            "status": status,
            "detail": detail,
            "created_at": created_at,
        }


def evidence_export(slug: str) -> dict | None:
    payload = load_scenario(slug)
    if payload is None:
        return None
    return {
        "evidence_type": "settlement_endpoint_control_tower_db_export",
        "generated_at": utc_now(),
        "scenario": payload["scenario"],
        "payment_context": payload["payment_context"],
        "checks": payload["checks"],
        "decision": payload["decision"],
        "operator_actions": payload["operator_actions"],
        "audit_events": payload["audit_events"],
        "source_manifest_summary": payload["source_manifest_summary"],
        "boundary": "synthetic_data_only_no_external_network_calls_no_proprietary_reference_rows",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SettlementEndpointDemoDB/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self) -> None:
        body = INDEX.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/":
                self.send_html()
            elif path == "/readyz":
                self.send_json(health())
            elif path == "/api/scenarios":
                self.send_json({"scenarios": scenario_list()})
            elif path.startswith("/api/evidence/"):
                slug = path.rsplit("/", 1)[-1]
                payload = evidence_export(slug)
                if payload is None:
                    self.send_json({"error": "scenario_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(payload)
            elif path.startswith("/api/scenarios/"):
                slug = path.rsplit("/", 1)[-1]
                payload = load_scenario(slug)
                if payload is None:
                    self.send_json({"error": "scenario_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(payload)
            elif path == "/api/audit/counts":
                self.send_json({"audit_counts": audit_counts()})
            elif path == "/api/source-manifest":
                self.send_json(load_source_manifest())
            else:
                self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except sqlite3.Error as exc:
            self.send_json({"status": "error", "database": "unreachable", "detail": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path.startswith("/api/scenarios/") and path.endswith("/actions"):
                parts = path.split("/")
                if len(parts) != 5:
                    self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
                    return
                raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
                payload = json.loads(raw_body.decode("utf-8") or "{}") if raw_body else {}
                action = record_operator_action(parts[3], payload)
                if action is None:
                    self.send_json({"error": "scenario_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json({"status": "action_recorded", "action": action}, HTTPStatus.CREATED)
            else:
                self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except json.JSONDecodeError:
            self.send_json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
        except sqlite3.Error as exc:
            self.send_json({"status": "error", "database": "unreachable", "detail": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the localhost-only DB-backed settlement endpoint demo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=4188, type=int)
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Refusing non-localhost bind for demo scaffold")
    if not DB_PATH.exists():
        raise SystemExit(f"Database missing. Run: python3 {ROOT / 'seed.py'}")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    host, port = httpd.server_address
    print(f"SERVING http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
