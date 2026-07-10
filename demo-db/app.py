#!/usr/bin/env python3
"""Localhost-only Stage 2 demo API for the Settlement Endpoint Control Tower."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from evaluator import evaluate

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "demo.sqlite"
INDEX = ROOT / "index.html"
MANIFEST_PATH = ROOT / "source_manifest.json"

# Input-boundary policy for the localhost operator-action write endpoint.
# The body carries exactly two short string fields, so the size bound is small.
MAX_ACTION_BODY_BYTES = 1024
ALLOWED_ACTION_TYPES = frozenset({"open_repair_task", "approve_fallback", "hold_payment"})
ALLOWED_ACTORS = frozenset({"demo_operator", "ops_analyst"})

# --- Endpoint-profile registry policy (SEC-P20) ----------------------------
# The single server-owned synthetic tenant. It is never client-supplied; every
# profile read/write/transition query is scoped through it. Must equal
# migrate.SYNTHETIC_TENANT_ID (a focused test pins that equality).
TENANT_ID = "synthetic-demo"

LIFECYCLE_DRAFT = "draft"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_SUPERSEDED = "superseded"

# The nested profile body carries four sections, so the size bound is larger than
# the action body -- but still bounded and rejected by declared length first.
MAX_PROFILE_BODY_BYTES = 4096
MAX_FIELD_LEN = 200

# Supported enum values, exactly the sets the evaluator understands. Any other
# value is unsupported and fails closed (no coercion).
AUTHORITY_STATES = frozenset({"current", "expiring_soon", "expired"})
ALLOWLIST_STATES = frozenset({"current", "stale"})
PAYLOAD_STATES = frozenset({"complete", "incomplete"})

# Ordered section schema: (section, required string fields, {enum field: allowed}).
# The order makes validation failures deterministic and stable.
PROFILE_SECTION_SPECS = (
    ("institution", ("name", "bic", "jurisdiction"), {}),
    ("legal_entity", ("name", "lei"), {"authority_status": AUTHORITY_STATES}),
    (
        "endpoint",
        ("wallet_address", "custody", "endpoint_owner", "requested_rail", "uetr"),
        {"allowlist_status": ALLOWLIST_STATES, "endpoint_payload_status": PAYLOAD_STATES},
    ),
    ("fallback", ("fallback_rail", "fallback_currency", "fallback_account_mask", "fallback_intermediary_bic"), {}),
)
PROFILE_SECTIONS = frozenset(name for name, _s, _e in PROFILE_SECTION_SPECS)

# Server-owned synthetic source lineage written for every created constituent row
# (returned parsed, as the existing APIs do). Never client-supplied.
LINEAGE_PROFILE_INSTITUTION = json.dumps({
    "kind": "source_lineage",
    "mode": "synthetic_operator_created_profile",
    "source_ids": ["iso_20022_concepts", "iban_structure_concepts"],
    "disclosure": "Synthetic institution profile created via the localhost endpoint-profile API; no external reference row copied.",
}, sort_keys=True)
LINEAGE_PROFILE_ENTITY = json.dumps({
    "kind": "source_lineage",
    "mode": "synthetic_operator_created_profile",
    "source_ids": ["lei_authority_concepts"],
    "disclosure": "Synthetic LEI/vLEI-style authority profile created via the localhost endpoint-profile API; no live GLEIF or vLEI verification claimed.",
}, sort_keys=True)
LINEAGE_PROFILE_ENDPOINT = json.dumps({
    "kind": "source_lineage",
    "mode": "synthetic_operator_created_profile",
    "source_ids": ["travel_rule_concepts", "iban_structure_concepts"],
    "disclosure": "Synthetic wallet and fallback account created via the localhost endpoint-profile API; IBAN/SSI semantics are shape-informed only.",
}, sort_keys=True)
PROFILE_INSTITUTION_ROLE = "creditor_agent"
PROFILE_INSTITUTION_REACHABILITY = "Synthetic institution evidence and fallback SSI holder."
PROFILE_AUTHORITY_DETAIL = "Synthetic vLEI-style authority evidence represented by the created profile."
PROFILE_MAINTAINER = "Synthetic treasury operations (localhost demo)."


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
            # Tenant-scoped like every other profile read: the app owns exactly one
            # synthetic tenant, so health reports only its profiles.
            "endpoint_profile_count": con.execute(
                "SELECT COUNT(*) FROM endpoint_profiles WHERE tenant_id = ?", (TENANT_ID,)
            ).fetchone()[0],
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


def record_operator_action(slug: str, action_type: str, actor: str) -> dict | None:
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


class RequestRejected(Exception):
    """A deterministic client-input rejection: an HTTP status and a stable code."""

    def __init__(self, status: HTTPStatus, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


def require_json_content_type(headers) -> None:
    raw = headers.get("Content-Type")
    if not raw:
        raise RequestRejected(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type")
    # Match the media type case-insensitively; allow parameters such as charset.
    media_type = raw.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise RequestRejected(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type")


# A bare loopback authority: exactly 127.0.0.1 or localhost with an optional
# decimal port. Anchored full-match over the RAW header keeps validation total
# and fail-closed -- urlparse silently strips tabs/CR/LF and tolerates trailing
# whitespace on the port, and empty ?/# delimiters parse to an empty (falsy)
# query/fragment, so a bare-origin check built on urlparse admits all of them.
_LOOPBACK_AUTHORITY_RE = re.compile(r"(?:127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?", re.IGNORECASE)


def _strict_loopback_authority(authority: str) -> str | None:
    """Return the lowercased authority iff it is exactly a bare loopback
    authority (127.0.0.1 or localhost) with an optional in-range decimal port,
    else None. Total over raw header text: any whitespace, control, comma list,
    empty/malformed/out-of-range port, or trailing delimiter yields None."""
    match = _LOOPBACK_AUTHORITY_RE.fullmatch(authority)
    if match is None:
        return None
    port = match.group(1)
    if port is not None and not (1 <= int(port) <= 65535):
        return None
    return authority.lower()


def require_allowed_origin(headers) -> None:
    origins = headers.get_all("Origin") or []
    if not origins:
        # application/json is a non-simple content type; a genuine non-browser
        # JSON client may legitimately omit Origin.
        return
    # A supplied Origin is fail-closed: exactly one Origin and exactly one Host,
    # both raw bare loopback authorities that match case-insensitively. Duplicate
    # Origin/Host (even when the first value is trusted) is rejected.
    hosts = headers.get_all("Host") or []
    if len(origins) != 1 or len(hosts) != 1:
        raise RequestRejected(HTTPStatus.FORBIDDEN, "origin_not_allowed")
    scheme = "http://"
    origin = origins[0]
    origin_authority = (
        _strict_loopback_authority(origin[len(scheme):]) if origin.startswith(scheme) else None
    )
    host_authority = _strict_loopback_authority(hosts[0])
    if origin_authority is None or host_authority is None or origin_authority != host_authority:
        raise RequestRejected(HTTPStatus.FORBIDDEN, "origin_not_allowed")


def _reject_non_rfc_json_constant(token: str) -> None:
    # RFC 8259 JSON has no NaN/Infinity/-Infinity. json.loads admits them by
    # default via parse_constant; refuse each like any other malformed JSON so a
    # non-finite float can never enter a persisted payload.
    raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_json")


def read_json_object(handler: BaseHTTPRequestHandler, max_bytes: int = MAX_ACTION_BODY_BYTES) -> dict:
    # Fail-closed HTTP framing: this fixed-length JSON endpoint accepts exactly
    # one Content-Length and no Transfer-Encoding. Ambiguous framing (any
    # Transfer-Encoding, or duplicate/conflicting Content-Length) is a request-
    # smuggling vector and is refused before the body is read. Inspect all
    # Content-Length instances, not only the first. ``max_bytes`` bounds the
    # declared length for this route (small for actions, larger for profiles).
    if handler.headers.get("Transfer-Encoding") is not None:
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_content_length")
    lengths = handler.headers.get_all("Content-Length") or []
    if len(lengths) > 1:
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_content_length")
    raw_length = lengths[0] if lengths else None
    if raw_length is None:
        raise RequestRejected(HTTPStatus.LENGTH_REQUIRED, "length_required")
    # HTTP Content-Length grammar is 1*DIGIT (ASCII 0-9). Require the full digit
    # grammar before int(), which would otherwise tolerate a leading '+'/'-',
    # underscore separators, or surrounding whitespace.
    if not (raw_length.isascii() and raw_length.isdigit()):
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_content_length")
    # Compare against the size bound WITHOUT converting an unbounded digit string
    # (int() raises past CPython's integer-string digit limit). Leading zeroes
    # are DIGIT grammar: normalize them, then compare by length, then lexically.
    normalized = raw_length.lstrip("0") or "0"
    max_str = str(max_bytes)
    if len(normalized) > len(max_str) or (len(normalized) == len(max_str) and normalized > max_str):
        # Reject by declared length before reading the body.
        raise RequestRejected(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large")
    declared = int(normalized)  # bounded: normalized <= max_bytes
    raw_body = handler.rfile.read(declared)
    if len(raw_body) < declared:
        # Peer closed before delivering the declared bytes.
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "incomplete_body")
    try:
        text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_utf8")
    try:
        payload = json.loads(text, parse_constant=_reject_non_rfc_json_constant)
    except json.JSONDecodeError:
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_json")
    if not isinstance(payload, dict):
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_payload")
    return payload


def validated_action(payload: dict) -> tuple[str, str]:
    action_type = payload.get("action_type")
    if not isinstance(action_type, str) or action_type not in ALLOWED_ACTION_TYPES:
        raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_action_type")
    actor = payload.get("actor")
    if not isinstance(actor, str) or actor not in ALLOWED_ACTORS:
        raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_actor")
    return action_type, actor


# --- Endpoint-profile registry service (SEC-P20) ---------------------------
# Transactional create / read / update / activate / supersede over first-class,
# multi-instance synthetic endpoint profiles. Every write validates the exact
# nested shape, is scoped through the server-owned tenant, and either fully
# persists or fully fails closed. Evaluation is computed live from the persisted
# field values via evaluator.evaluate -- never from a scenario slug.


def _valid_field_string(value: object) -> bool:
    # A required, non-empty, bounded, non-blank string. No coercion.
    return isinstance(value, str) and 1 <= len(value) <= MAX_FIELD_LEN and value.strip() != ""


def _validate_section(section: object, string_fields: tuple, enum_fields: dict) -> dict:
    if not isinstance(section, dict):
        raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_field")
    allowed = set(string_fields) | set(enum_fields)
    keys = set(section)
    if keys - allowed:  # reject unknown/extra structure, never silently ignore
        raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "unknown_field")
    if allowed - keys:
        raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "missing_field")
    out = {}
    for field in string_fields:
        value = section[field]
        if not _valid_field_string(value):
            raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_field")
        out[field] = value
    for field, allowed_values in enum_fields.items():
        value = section[field]
        if not isinstance(value, str):
            raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_field")
        if value not in allowed_values:
            raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "unsupported_enum")
        out[field] = value
    return out


def validate_profile_payload(payload: dict) -> dict:
    """Validate the exact nested profile shape, failing closed with a stable code.

    Rejects unknown/extra structure, missing required sections/fields, wrong
    types, blank or overlong strings, and unsupported enum values -- without
    coercion. Returns the four validated sections.
    """
    if not isinstance(payload, dict):
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_payload")
    keys = set(payload)
    if keys - PROFILE_SECTIONS:
        raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "unknown_field")
    if PROFILE_SECTIONS - keys:
        raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "missing_field")
    return {
        name: _validate_section(payload[name], string_fields, enum_fields)
        for name, string_fields, enum_fields in PROFILE_SECTION_SPECS
    }


def _strict_positive_int(value: object) -> bool:
    # A bounded positive integer; bool is excluded (JSON true/false are ints).
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 2**63 - 1


def validated_replacement(payload: dict) -> int:
    """Extract a strict positive-integer replacement_id from a supersession body."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"replacement_id"}
        or not _strict_positive_int(payload["replacement_id"])
    ):
        raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_replacement")
    return payload["replacement_id"]


def parse_profile_id(segment: str) -> int:
    """Parse a URL path id strictly: ASCII digits, no leading zero, bounded, >= 1.

    The 64-bit upper bound is enforced by DIGIT length/lexicographic comparison
    BEFORE int(): int() raises past CPython's integer-string digit limit (3.11+
    and 3.9.14+ backports), so an unbounded conversion of a multi-thousand-digit
    id would surface as an uncaught 500 instead of a deterministic 400.
    """
    if not (segment.isascii() and segment.isdigit()):
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_profile_id")
    if len(segment) > 1 and segment[0] == "0":
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_profile_id")
    # segment is now bare ASCII digits with no leading zero, so a length-then-
    # lexicographic compare against the 64-bit signed max is exact and total.
    max_str = str(2**63 - 1)
    if len(segment) > len(max_str) or (len(segment) == len(max_str) and segment > max_str):
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_profile_id")
    value = int(segment)  # bounded: segment <= 2**63 - 1, safe for int()
    if value < 1:
        raise RequestRejected(HTTPStatus.BAD_REQUEST, "invalid_profile_id")
    return value


def profile_rule_input(entity: sqlite3.Row, endpoint: sqlite3.Row) -> dict:
    """Map a profile's persisted constituent fields to evaluator rule input.

    Institution presence/reachability and legal-entity presence are structural (a
    well-formed profile always has them); the graded fields come straight from the
    persisted legal-entity and settlement-endpoint rows, so each profile is
    evaluated independently with no shared or slug-derived state.
    """
    return {
        "institution_present": True,
        "institution_reachable": True,
        "legal_entity_present": True,
        "authority_status": entity["authority_status"],
        "allowlist_status": endpoint["allowlist_status"],
        "payload_status": endpoint["endpoint_payload_status"],
        "fallback_rail": endpoint["fallback_rail"],
        "fallback_currency": endpoint["fallback_currency"],
        "fallback_account_mask": endpoint["fallback_account_mask"],
        "fallback_intermediary_bic": endpoint["fallback_intermediary_bic"],
    }


def _profile_view(profile: sqlite3.Row) -> dict:
    return {
        "id": profile["id"],
        "tenant_id": profile["tenant_id"],
        "endpoint_id": profile["endpoint_id"],
        "lifecycle_state": profile["lifecycle_state"],
        "superseded_by": profile["superseded_by"],
        "created_at": profile["created_at"],
        "updated_at": profile["updated_at"],
    }


def load_profile(profile_id: int) -> dict | None:
    """Read one tenant-scoped profile with its constituents and live evaluation."""
    with get_db() as con:
        profile = con.execute(
            "SELECT * FROM endpoint_profiles WHERE id = ? AND tenant_id = ?", (profile_id, TENANT_ID)
        ).fetchone()
        if not profile:
            return None
        endpoint = con.execute("SELECT * FROM settlement_endpoints WHERE id = ?", (profile["endpoint_id"],)).fetchone()
        entity = con.execute("SELECT * FROM legal_entities WHERE id = ?", (endpoint["legal_entity_id"],)).fetchone()
        institution = con.execute("SELECT * FROM institutions WHERE id = ?", (entity["institution_id"],)).fetchone()
        return {
            "profile": _profile_view(profile),
            "institution": row_dict(institution),
            "legal_entity": row_dict(entity),
            "endpoint": row_dict(endpoint),
            "evaluation": evaluate(profile_rule_input(entity, endpoint)),
            "boundary": "synthetic_data_only_no_external_network_calls",
        }


def list_profiles() -> list[dict]:
    """List every tenant-scoped profile with its lifecycle state and live verdict."""
    with get_db() as con:
        profiles = con.execute(
            "SELECT * FROM endpoint_profiles WHERE tenant_id = ? ORDER BY id", (TENANT_ID,)
        ).fetchall()
        items = []
        for profile in profiles:
            endpoint = con.execute("SELECT * FROM settlement_endpoints WHERE id = ?", (profile["endpoint_id"],)).fetchone()
            entity = con.execute("SELECT * FROM legal_entities WHERE id = ?", (endpoint["legal_entity_id"],)).fetchone()
            institution = con.execute("SELECT * FROM institutions WHERE id = ?", (entity["institution_id"],)).fetchone()
            evaluation = evaluate(profile_rule_input(entity, endpoint))
            items.append({
                "id": profile["id"],
                "lifecycle_state": profile["lifecycle_state"],
                "endpoint_id": profile["endpoint_id"],
                "superseded_by": profile["superseded_by"],
                "bic": institution["bic"],
                "lei": entity["lei"],
                "uetr": endpoint["uetr"],
                "verdict": evaluation["decision"]["verdict"],
                "created_at": profile["created_at"],
                "updated_at": profile["updated_at"],
            })
        return items


def _assert_unique(con: sqlite3.Connection, table: str, column: str, value: str, error: str, exclude_id: int | None = None) -> None:
    # table/column are fixed internal literals, never caller-derived.
    if exclude_id is None:
        row = con.execute(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (value,)).fetchone()
    else:
        row = con.execute(f"SELECT 1 FROM {table} WHERE {column} = ? AND id != ? LIMIT 1", (value, exclude_id)).fetchone()
    if row:
        raise RequestRejected(HTTPStatus.CONFLICT, error)


def create_profile(sections: dict) -> dict:
    """Transactionally create a DRAFT profile and its constituent rows."""
    now = utc_now()
    inst, le, ep, fb = sections["institution"], sections["legal_entity"], sections["endpoint"], sections["fallback"]
    with get_db() as con:
        try:
            with con:  # one transaction: all four rows persist or none do
                _assert_unique(con, "institutions", "bic", inst["bic"], "duplicate_bic")
                _assert_unique(con, "legal_entities", "lei", le["lei"], "duplicate_lei")
                _assert_unique(con, "settlement_endpoints", "uetr", ep["uetr"], "duplicate_uetr")
                inst_id = con.execute(
                    "INSERT INTO institutions(role, name, bic, jurisdiction, reachability, source_lineage) VALUES (?,?,?,?,?,?)",
                    (PROFILE_INSTITUTION_ROLE, inst["name"], inst["bic"], inst["jurisdiction"], PROFILE_INSTITUTION_REACHABILITY, LINEAGE_PROFILE_INSTITUTION),
                ).lastrowid
                le_id = con.execute(
                    "INSERT INTO legal_entities(institution_id, name, lei, authority_status, authority_detail, maintainer, source_lineage) VALUES (?,?,?,?,?,?,?)",
                    (inst_id, le["name"], le["lei"], le["authority_status"], PROFILE_AUTHORITY_DETAIL, PROFILE_MAINTAINER, LINEAGE_PROFILE_ENTITY),
                ).lastrowid
                ep_id = con.execute(
                    "INSERT INTO settlement_endpoints(legal_entity_id, wallet_address, custody, allowlist_status, endpoint_owner, endpoint_payload_status, requested_rail, fallback_rail, fallback_currency, fallback_account_mask, fallback_intermediary_bic, uetr, source_lineage)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (le_id, ep["wallet_address"], ep["custody"], ep["allowlist_status"], ep["endpoint_owner"], ep["endpoint_payload_status"], ep["requested_rail"], fb["fallback_rail"], fb["fallback_currency"], fb["fallback_account_mask"], fb["fallback_intermediary_bic"], ep["uetr"], LINEAGE_PROFILE_ENDPOINT),
                ).lastrowid
                profile_id = con.execute(
                    "INSERT INTO endpoint_profiles(tenant_id, endpoint_id, lifecycle_state, superseded_by, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (TENANT_ID, ep_id, LIFECYCLE_DRAFT, None, now, now),
                ).lastrowid
        except sqlite3.IntegrityError:
            # A concurrent create racing the same unique value: fail closed with a
            # stable conflict, never a partial write (the transaction rolled back).
            raise RequestRejected(HTTPStatus.CONFLICT, "conflict")
        return load_profile(profile_id)


def update_profile(profile_id: int, sections: dict) -> dict | None:
    """Transactionally replace a DRAFT profile's constituent fields.

    Active or superseded profiles are immutable through this API; the update fails
    closed with no write.
    """
    now = utc_now()
    inst, le, ep, fb = sections["institution"], sections["legal_entity"], sections["endpoint"], sections["fallback"]
    with get_db() as con:
        try:
            with con:
                # Take the write lock before the draft precondition read so the
                # check and the constituent rewrites are one serialized transaction.
                # Otherwise a competing activation can linearize between this read
                # and the writes, and the update would then mutate an already-active
                # profile; BEGIN IMMEDIATE forecloses that window and a competing
                # activation re-reads the draft state under its own lock.
                con.execute("BEGIN IMMEDIATE")
                profile = con.execute(
                    "SELECT * FROM endpoint_profiles WHERE id = ? AND tenant_id = ?", (profile_id, TENANT_ID)
                ).fetchone()
                if not profile:
                    return None
                if profile["lifecycle_state"] != LIFECYCLE_DRAFT:
                    raise RequestRejected(HTTPStatus.CONFLICT, "profile_not_draft")
                endpoint = con.execute("SELECT * FROM settlement_endpoints WHERE id = ?", (profile["endpoint_id"],)).fetchone()
                entity = con.execute("SELECT * FROM legal_entities WHERE id = ?", (endpoint["legal_entity_id"],)).fetchone()
                _assert_unique(con, "institutions", "bic", inst["bic"], "duplicate_bic", exclude_id=entity["institution_id"])
                _assert_unique(con, "legal_entities", "lei", le["lei"], "duplicate_lei", exclude_id=entity["id"])
                _assert_unique(con, "settlement_endpoints", "uetr", ep["uetr"], "duplicate_uetr", exclude_id=endpoint["id"])
                con.execute(
                    "UPDATE institutions SET name=?, bic=?, jurisdiction=? WHERE id=?",
                    (inst["name"], inst["bic"], inst["jurisdiction"], entity["institution_id"]),
                )
                con.execute(
                    "UPDATE legal_entities SET name=?, lei=?, authority_status=? WHERE id=?",
                    (le["name"], le["lei"], le["authority_status"], entity["id"]),
                )
                con.execute(
                    "UPDATE settlement_endpoints SET wallet_address=?, custody=?, allowlist_status=?, endpoint_owner=?, endpoint_payload_status=?, requested_rail=?, fallback_rail=?, fallback_currency=?, fallback_account_mask=?, fallback_intermediary_bic=?, uetr=? WHERE id=?",
                    (ep["wallet_address"], ep["custody"], ep["allowlist_status"], ep["endpoint_owner"], ep["endpoint_payload_status"], ep["requested_rail"], fb["fallback_rail"], fb["fallback_currency"], fb["fallback_account_mask"], fb["fallback_intermediary_bic"], ep["uetr"], endpoint["id"]),
                )
                con.execute("UPDATE endpoint_profiles SET updated_at=? WHERE id=?", (now, profile_id))
        except sqlite3.IntegrityError:
            raise RequestRejected(HTTPStatus.CONFLICT, "conflict")
        return load_profile(profile_id)


def activate_profile(profile_id: int) -> dict | None:
    """Transition a DRAFT profile to ACTIVE. Any other state fails closed."""
    now = utc_now()
    with get_db() as con:
        with con:
            # Acquire the write lock BEFORE the precondition read so the state
            # check and the transition write are one serialized transaction. With
            # ThreadingHTTPServer a deferred transaction would read the lifecycle
            # state outside any lock, letting two concurrent activations both pass
            # the draft check and both write; BEGIN IMMEDIATE serializes them so
            # the loser re-reads the linearized state and fails closed. Rollback
            # still flows through the existing connection context.
            con.execute("BEGIN IMMEDIATE")
            profile = con.execute(
                "SELECT * FROM endpoint_profiles WHERE id = ? AND tenant_id = ?", (profile_id, TENANT_ID)
            ).fetchone()
            if not profile:
                return None
            if profile["lifecycle_state"] != LIFECYCLE_DRAFT:
                raise RequestRejected(HTTPStatus.CONFLICT, "invalid_transition")
            con.execute(
                "UPDATE endpoint_profiles SET lifecycle_state=?, updated_at=? WHERE id=?",
                (LIFECYCLE_ACTIVE, now, profile_id),
            )
        return load_profile(profile_id)


def supersede_profile(profile_id: int, replacement_id: int) -> dict | None:
    """Atomically supersede an ACTIVE profile with a DRAFT replacement.

    The active profile becomes superseded (linked to its replacement) and the
    draft replacement becomes active, in one transaction. A malformed relationship
    (self, non-active source, missing or non-draft replacement) fails closed with
    no partial write. There is no transition out of superseded, and the old
    profile and all of its history are preserved.
    """
    now = utc_now()
    with get_db() as con:
        with con:
            # Serialize the whole replacement (both state reads and both writes) so
            # two concurrent supersessions of the same active source cannot each
            # promote a different draft replacement. BEGIN IMMEDIATE takes the write
            # lock before the state reads; the loser re-reads the now-superseded
            # source and fails closed, leaving its replacement a draft. Rollback
            # flows through the existing connection context.
            con.execute("BEGIN IMMEDIATE")
            active = con.execute(
                "SELECT * FROM endpoint_profiles WHERE id = ? AND tenant_id = ?", (profile_id, TENANT_ID)
            ).fetchone()
            if not active:
                return None
            if active["lifecycle_state"] != LIFECYCLE_ACTIVE:
                raise RequestRejected(HTTPStatus.CONFLICT, "invalid_transition")
            if replacement_id == profile_id:
                raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_replacement")
            replacement = con.execute(
                "SELECT * FROM endpoint_profiles WHERE id = ? AND tenant_id = ?", (replacement_id, TENANT_ID)
            ).fetchone()
            if not replacement:
                raise RequestRejected(HTTPStatus.NOT_FOUND, "replacement_not_found")
            if replacement["lifecycle_state"] != LIFECYCLE_DRAFT:
                raise RequestRejected(HTTPStatus.CONFLICT, "invalid_transition")
            con.execute(
                "UPDATE endpoint_profiles SET lifecycle_state=?, superseded_by=?, updated_at=? WHERE id=?",
                (LIFECYCLE_SUPERSEDED, replacement_id, now, profile_id),
            )
            con.execute(
                "UPDATE endpoint_profiles SET lifecycle_state=?, updated_at=? WHERE id=?",
                (LIFECYCLE_ACTIVE, now, replacement_id),
            )
        return {
            "status": "superseded",
            "profile": load_profile(profile_id),
            "replacement": load_profile(replacement_id),
        }


# --- Structured local request/error logging (SEC-O20) -----------------------
# Diagnosable JSON Lines on stderr using only the standard library. Every record
# is built from a fixed field allowlist, so no request body, response payload,
# query string/value, header value, actor/action field value, SQL, exception
# message, or other sensitive identifier can enter a log line. The SERVING
# readiness banner stays on stdout and is unchanged.

_FIXED_ROUTES = frozenset(
    {"/", "/readyz", "/api/scenarios", "/api/audit/counts", "/api/source-manifest", "/api/endpoint-profiles"}
)


def normalized_route(raw_target: str) -> str:
    """Map a raw request target to a stable, non-sensitive route label.

    The query string is dropped and the single variable slug segment is
    collapsed to a ``{slug}`` placeholder, so no caller-controlled or sensitive
    path/query value can reach a log line; unrecognized targets bucket to a
    fixed sentinel. Total over arbitrary or malformed input.
    """
    try:
        path = urlparse(raw_target or "").path.rstrip("/") or "/"
    except ValueError:
        # Malformed target (e.g. an unmatched IPv6 bracket makes urlparse raise);
        # stay total and never echo the caller-controlled raw text.
        return "/{other}"
    if path in _FIXED_ROUTES:
        return path
    segments = path.count("/")
    if path.startswith("/api/scenarios/") and path.endswith("/actions") and segments == 4:
        return "/api/scenarios/{slug}/actions"
    if path.startswith("/api/evidence/") and segments == 3:
        return "/api/evidence/{slug}"
    if path.startswith("/api/scenarios/") and segments == 3:
        return "/api/scenarios/{slug}"
    if path.startswith("/api/endpoint-profiles/") and path.endswith("/activation") and segments == 4:
        return "/api/endpoint-profiles/{id}/activation"
    if path.startswith("/api/endpoint-profiles/") and path.endswith("/supersession") and segments == 4:
        return "/api/endpoint-profiles/{id}/supersession"
    if path.startswith("/api/endpoint-profiles/") and segments == 3:
        return "/api/endpoint-profiles/{id}"
    return "/{other}"


# Finite allowlist of standard HTTP method names; any caller-selected method
# token is collapsed to a fixed fallback so it is never reproduced in a log line.
_ALLOWED_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)


def _allowed_method(command: object) -> str:
    return command if isinstance(command, str) and command in _ALLOWED_METHODS else "OTHER"


def _emit(record: dict) -> None:
    # One compact JSON object per line on stderr, flushed so local tooling and
    # tests observe each event immediately.
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    stream = sys.stderr
    stream.write(line + "\n")
    stream.flush()


def log_request_event(method: str, path: str, status: object, elapsed_ms: object = None) -> None:
    """Emit one access-log event with only allowlisted, non-sensitive fields."""
    record = {
        "ts": utc_now(),
        "level": "info",
        "event": "request",
        "method": method,
        "path": path,
        "status": status,
    }
    if elapsed_ms is not None:
        record["elapsed_ms"] = elapsed_ms
    _emit(record)


def log_error_event(method: str, path: str, category: str, exc: BaseException, status: int = 500) -> None:
    """Emit one error event with a stable category and the exception class name.

    Never records ``str(exc)``/``repr(exc)`` or any request/response content.
    """
    _emit(
        {
            "ts": utc_now(),
            "level": "error",
            "event": "error",
            "method": method,
            "path": path,
            "status": status,
            "category": category,
            "exc_type": type(exc).__name__,
        }
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "SettlementEndpointDemoDB/1.0"

    def handle_one_request(self) -> None:  # noqa: N802 - stdlib handler API
        # Stamp a monotonic start so log_request can report elapsed time.
        self._request_start = time.monotonic()
        super().handle_one_request()

    def _request_context(self):
        # Safe (method, normalized-route) pair for logging. Method is an
        # allowlisted standard method or "OTHER"; the path comes from the raw
        # request line (always a str, never the possibly-unset/stale self.path)
        # and is collapsed to a stable route label with the query string dropped.
        method = _allowed_method(getattr(self, "command", None))
        requestline = getattr(self, "requestline", "") or ""
        parts = requestline.split()
        target = parts[1] if len(parts) >= 2 else ""
        return method, normalized_route(target)

    def log_request(self, code: object = "-", size: object = "-") -> None:  # noqa: N802 - stdlib API
        # Structured access log for every response (normal, client rejection,
        # and framework error), replacing the default that echoes the raw
        # request line -- including the query string -- to stderr.
        method, path = self._request_context()
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = None
        start = getattr(self, "_request_start", None)
        elapsed_ms = round((time.monotonic() - start) * 1000) if start is not None else None
        log_request_event(method, path, status, elapsed_ms)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: N802 - stdlib API
        # Silence the default stderr sink; structured events are emitted via
        # log_request / log_error_event instead. This also suppresses the
        # framework's log_error text (it routes through log_message).
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
        try:
            # Parse inside the try so a malformed target (urlparse ValueError)
            # enters the deterministic structured internal-error path below.
            path = urlparse(self.path).path.rstrip("/") or "/"
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
            elif path == "/api/endpoint-profiles":
                self.send_json({"endpoint_profiles": list_profiles()})
            elif path.startswith("/api/endpoint-profiles/"):
                parts = path.split("/")
                if len(parts) != 4:
                    self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    payload = load_profile(parse_profile_id(parts[3]))
                    if payload is None:
                        self.send_json({"error": "profile_not_found"}, HTTPStatus.NOT_FOUND)
                    else:
                        self.send_json(payload)
            else:
                self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except RequestRejected as exc:
            # A strict numeric-id rejection on a read path: stable code, logged
            # with the code as category (a fixed string, never caller-derived).
            method, path = self._request_context()
            log_error_event(method, path, exc.error, exc, status=int(exc.status))
            self.send_json({"error": exc.error}, exc.status)
        except sqlite3.Error as exc:
            method, path = self._request_context()
            log_error_event(method, path, "database_error", exc)
            self.send_json(
                {"status": "error", "database": "unreachable", "detail": "database_error"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        except Exception as exc:  # noqa: BLE001 - deterministic 500, never leak a traceback
            method, path = self._request_context()
            log_error_event(method, path, "internal_error", exc)
            try:
                self.send_json({"status": "error", "detail": "internal_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:  # noqa: BLE001 - response channel already broken
                pass

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            # Parse inside the try so a malformed target (urlparse ValueError)
            # enters the deterministic structured internal-error path below.
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/api/endpoint-profiles":
                require_json_content_type(self.headers)
                require_allowed_origin(self.headers)
                payload = read_json_object(self, MAX_PROFILE_BODY_BYTES)
                self.send_json(create_profile(validate_profile_payload(payload)), HTTPStatus.CREATED)
            elif path.startswith("/api/endpoint-profiles/") and path.endswith("/activation"):
                parts = path.split("/")
                if len(parts) != 5:
                    self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
                    return
                require_json_content_type(self.headers)
                require_allowed_origin(self.headers)
                profile_id = parse_profile_id(parts[3])
                # Activation takes no parameters; require an empty JSON object so
                # the strict-json write boundary still applies. Any content is rejected.
                if read_json_object(self, MAX_ACTION_BODY_BYTES) != {}:
                    raise RequestRejected(HTTPStatus.UNPROCESSABLE_ENTITY, "unknown_field")
                result = activate_profile(profile_id)
                if result is None:
                    self.send_json({"error": "profile_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(result)
            elif path.startswith("/api/endpoint-profiles/") and path.endswith("/supersession"):
                parts = path.split("/")
                if len(parts) != 5:
                    self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
                    return
                require_json_content_type(self.headers)
                require_allowed_origin(self.headers)
                profile_id = parse_profile_id(parts[3])
                replacement_id = validated_replacement(read_json_object(self, MAX_ACTION_BODY_BYTES))
                result = supersede_profile(profile_id, replacement_id)
                if result is None:
                    self.send_json({"error": "profile_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(result)
            elif path.startswith("/api/scenarios/") and path.endswith("/actions"):
                parts = path.split("/")
                if len(parts) != 5:
                    self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
                    return
                require_json_content_type(self.headers)
                require_allowed_origin(self.headers)
                payload = read_json_object(self)
                action_type, actor = validated_action(payload)
                action = record_operator_action(parts[3], action_type, actor)
                if action is None:
                    self.send_json({"error": "scenario_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json({"status": "action_recorded", "action": action}, HTTPStatus.CREATED)
            else:
                self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except RequestRejected as exc:
            # Log the rejection with its existing stable code as the category so
            # same-status rejection classes stay individually diagnosable; the
            # category is a fixed hardcoded string, never derived from raw text.
            method, path = self._request_context()
            log_error_event(method, path, exc.error, exc, status=int(exc.status))
            self.send_json({"error": exc.error}, exc.status)
        except sqlite3.Error as exc:
            method, path = self._request_context()
            log_error_event(method, path, "database_error", exc)
            self.send_json(
                {"status": "error", "database": "unreachable", "detail": "database_error"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        except Exception as exc:  # noqa: BLE001 - deterministic 500, never leak a traceback
            method, path = self._request_context()
            log_error_event(method, path, "internal_error", exc)
            try:
                self.send_json({"status": "error", "detail": "internal_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:  # noqa: BLE001 - response channel already broken
                pass

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            # Parse inside the try so a malformed target (urlparse ValueError)
            # enters the deterministic structured internal-error path below.
            path = urlparse(self.path).path.rstrip("/") or "/"
            parts = path.split("/")
            if path.startswith("/api/endpoint-profiles/") and len(parts) == 4:
                require_json_content_type(self.headers)
                require_allowed_origin(self.headers)
                profile_id = parse_profile_id(parts[3])
                payload = read_json_object(self, MAX_PROFILE_BODY_BYTES)
                result = update_profile(profile_id, validate_profile_payload(payload))
                if result is None:
                    self.send_json({"error": "profile_not_found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(result)
            else:
                self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except RequestRejected as exc:
            method, path = self._request_context()
            log_error_event(method, path, exc.error, exc, status=int(exc.status))
            self.send_json({"error": exc.error}, exc.status)
        except sqlite3.Error as exc:
            method, path = self._request_context()
            log_error_event(method, path, "database_error", exc)
            self.send_json(
                {"status": "error", "database": "unreachable", "detail": "database_error"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        except Exception as exc:  # noqa: BLE001 - deterministic 500, never leak a traceback
            method, path = self._request_context()
            log_error_event(method, path, "internal_error", exc)
            try:
                self.send_json({"status": "error", "detail": "internal_error"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:  # noqa: BLE001 - response channel already broken
                pass

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        # There is no delete route: endpoint-profile history (scenario, decision,
        # audit, action, and source lineage) is preserved. A profile detail
        # resource answers 405 method_not_allowed; anything else is 404. No row is
        # ever deleted.
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
        except ValueError:
            self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        parts = path.split("/")
        if path.startswith("/api/endpoint-profiles/") and len(parts) == 4:
            self.send_json({"error": "method_not_allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)
        else:
            self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)


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
