#!/usr/bin/env python3
"""SEC-P20 tests: first-class multi-instance synthetic endpoint profiles.

These pin the contract of the endpoint-profile registry and its controlled
create / read / update / activate / supersede lifecycle over the real localhost
API (``demo-db/app.py``) and the ``endpoint_profiles`` schema.

Every case runs against a freshly reseeded synthetic SQLite database and drives
the actual HTTP surface (no scenario slug shortcut). The lifecycle vocabulary is
exactly ``draft`` / ``active`` / ``superseded``; legal transitions are only
``draft -> active`` and an atomic ``active -> superseded`` replacement.

Contract matrix (endpoint -> deterministic outcome):

    | route                                   | method | success | key rejections                    |
    |-----------------------------------------|--------|---------|-----------------------------------|
    | /api/endpoint-profiles                  | POST   |   201   | shape/field/enum/dup -> 4xx, no wr |
    | /api/endpoint-profiles                  | GET    |   200   | -                                 |
    | /api/endpoint-profiles/{id}             | GET    |   200   | bad id 400, absent 404            |
    | /api/endpoint-profiles/{id}             | PUT    |   200   | non-draft 409, dup 409            |
    | /api/endpoint-profiles/{id}/activation  | POST   |   200   | non-draft 409 invalid_transition  |
    | /api/endpoint-profiles/{id}/supersession| POST   |   200   | self/state/replacement -> 4xx     |
    | /api/endpoint-profiles/{id}             | DELETE |   404/405 (no delete route)                  |

All fixtures are synthetic, localhost-only, and make no external network calls.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections import namedtuple
from pathlib import Path
from unittest import mock

import app
import seed

_REAL_INT = int


def _digit_capped_int(value, *args, **kwargs):
    """Mimic CPython's integer-string digit cap (3.11+, 3.9.14+ backports).

    On the local interpreter the cap may be inactive, which would mask the
    fail-closed gap; this makes the guarantee provable on every interpreter.
    """
    if isinstance(value, str) and len(value.lstrip("+-")) > 4300:
        raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")
    return _REAL_INT(value, *args, **kwargs)

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "seed.py"
APP = ROOT / "app.py"
RAW_TIMEOUT = 4.0

APPROVED = "TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED"
BLOCKED = "TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED"
AUTHORITY_HOLD = "AUTHORITY_EXPIRED_MANUAL_HOLD"
INSUFFICIENT_HOLD = "INSUFFICIENT_INPUT_MANUAL_HOLD"

# 10 distinct synthetic profile shapes: distinct identities/coordinates and a
# verdict that follows only from the persisted field values. All four verdicts
# are represented, proving independent evaluation with no shared fixture state.
PROFILE_MATRIX = [
    dict(authority="current", allowlist="current", payload="complete", fallback="good", verdict=APPROVED),
    dict(authority="current", allowlist="stale", payload="complete", fallback="good", verdict=BLOCKED),
    dict(authority="current", allowlist="current", payload="incomplete", fallback="good", verdict=BLOCKED),
    dict(authority="expired", allowlist="current", payload="complete", fallback="good", verdict=AUTHORITY_HOLD),
    dict(authority="expiring_soon", allowlist="current", payload="complete", fallback="good", verdict=INSUFFICIENT_HOLD),
    dict(authority="current", allowlist="current", payload="complete", fallback="bad", verdict=INSUFFICIENT_HOLD),
    dict(authority="expired", allowlist="stale", payload="complete", fallback="good", verdict=BLOCKED),
    dict(authority="current", allowlist="stale", payload="incomplete", fallback="good", verdict=BLOCKED),
    dict(authority="expired", allowlist="current", payload="incomplete", fallback="good", verdict=BLOCKED),
    dict(authority="current", allowlist="current", payload="complete", fallback="good", verdict=APPROVED),
]

Response = namedtuple("Response", ["status", "headers", "text", "json"])


def profile_payload(index, *, authority="current", allowlist="current", payload="complete", fallback="good", **_):
    """A fully-formed, distinct synthetic profile body keyed by ``index``."""
    tag = f"{index:03d}"
    fb = {
        "fallback_rail": "Fiat SSI route",
        "fallback_currency": "EUR",
        "fallback_account_mask": "DE•• •••• •••• 4400",
        "fallback_intermediary_bic": "INTERDEFFXXX",
    }
    if fallback == "bad":
        # Non-empty but unsupported synthetic value: a legal draft that the
        # evaluator holds because the fiat fallback is not traceable.
        fb["fallback_currency"] = "USD"
    return {
        "institution": {
            "name": f"Synthetic Institution {tag}",
            "bic": f"SYNBIC{tag}XXX",
            "jurisdiction": "EU synthetic profile",
        },
        "legal_entity": {
            "name": f"Synthetic Entity {tag}",
            "lei": f"SYNTHLEI0000000{tag}",
            "authority_status": authority,
        },
        "endpoint": {
            "wallet_address": f"0xSYN{tag}",
            "custody": "Approved custodian",
            "allowlist_status": allowlist,
            "endpoint_owner": "Treasury ops queue",
            "endpoint_payload_status": payload,
            "requested_rail": "Tokenized deposit",
            "uetr": f"SYN-PROFILE-{tag}",
        },
        "fallback": fb,
    }


def _parse_response(raw: bytes) -> Response:
    if not raw:
        return Response(None, {}, "", None)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = None
    if lines:
        parts = lines[0].split(b" ")
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, _, value = line.partition(b":")
            headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    text = body.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return Response(status, headers, text, parsed)


class NormalizedRouteProfileTests(unittest.TestCase):
    """The logged path label collapses profile ids and never leaks raw text."""

    def test_collection_route_is_fixed(self) -> None:
        self.assertEqual(app.normalized_route("/api/endpoint-profiles"), "/api/endpoint-profiles")

    def test_detail_route_collapses_id(self) -> None:
        self.assertEqual(app.normalized_route("/api/endpoint-profiles/7"), "/api/endpoint-profiles/{id}")
        self.assertEqual(app.normalized_route("/api/endpoint-profiles/7/"), "/api/endpoint-profiles/{id}")

    def test_transition_routes_collapse_id(self) -> None:
        self.assertEqual(
            app.normalized_route("/api/endpoint-profiles/12/activation"),
            "/api/endpoint-profiles/{id}/activation",
        )
        self.assertEqual(
            app.normalized_route("/api/endpoint-profiles/12/supersession"),
            "/api/endpoint-profiles/{id}/supersession",
        )

    def test_query_and_id_never_leak(self) -> None:
        label = app.normalized_route("/api/endpoint-profiles/999?token=SENTINEL-DO-NOT-LOG")
        self.assertEqual(label, "/api/endpoint-profiles/{id}")
        self.assertNotIn("SENTINEL-DO-NOT-LOG", label)
        self.assertNotIn("999", label)


class TenantConstantTests(unittest.TestCase):
    def test_app_tenant_is_the_single_server_owned_constant(self) -> None:
        import migrate

        self.assertEqual(app.TENANT_ID, "synthetic-demo")
        self.assertEqual(app.TENANT_ID, migrate.SYNTHETIC_TENANT_ID)


class ProfileIdBoundTests(unittest.TestCase):
    """parse_profile_id must reject an overlong numeric id BEFORE int(): int()
    raises past CPython's integer-string digit cap, which would otherwise turn a
    multi-thousand-digit id into an uncaught 500 rather than a clean 400."""

    def test_overlong_numeric_id_rejected_before_int_conversion(self) -> None:
        huge = "9" * 5000
        with mock.patch("builtins.int", _digit_capped_int):
            with self.assertRaises(app.RequestRejected) as cm:
                app.parse_profile_id(huge)
        self.assertEqual(cm.exception.error, "invalid_profile_id")
        self.assertEqual(int(cm.exception.status), 400)

    def test_max_int64_id_still_parses(self) -> None:
        self.assertEqual(app.parse_profile_id(str(2 ** 63 - 1)), 2 ** 63 - 1)

    def test_one_past_int64_rejected(self) -> None:
        with self.assertRaises(app.RequestRejected) as cm:
            app.parse_profile_id(str(2 ** 63))
        self.assertEqual(cm.exception.error, "invalid_profile_id")


class _ProfileServerCase(unittest.TestCase):
    """Reseed the synthetic DB, run app.py, and capture stderr for log checks."""

    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run([sys.executable, str(SEED)], check=True, text=True, capture_output=True)
        cls.host = "127.0.0.1"
        cls.port = cls._free_port()
        cls.base = f"http://{cls.host}:{cls.port}"
        cls.origin = f"http://{cls.host}:{cls.port}"
        cls._err = tempfile.NamedTemporaryFile(mode="wb", suffix=".err", delete=False)
        cls.err_path = Path(cls._err.name)
        cls.proc = subprocess.Popen(
            [sys.executable, str(APP), "--host", cls.host, "--port", str(cls.port)],
            stdout=subprocess.PIPE,
            stderr=cls._err,
        )
        cls._wait_ready()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=RAW_TIMEOUT)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=RAW_TIMEOUT)
        try:
            cls._err.close()
        except OSError:
            pass
        try:
            cls.err_path.unlink()
        except OSError:
            pass
        # Restore a clean synthetic DB for any downstream suite.
        subprocess.run([sys.executable, str(SEED)], check=True, text=True, capture_output=True)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @classmethod
    def _wait_ready(cls) -> None:
        last: Exception | None = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"{cls.base}/readyz", timeout=RAW_TIMEOUT) as resp:
                    if json.loads(resp.read().decode("utf-8")).get("status") == "ok":
                        return
            except Exception as exc:  # noqa: BLE001 - surface final error after retries
                last = exc
                time.sleep(0.1)
        raise RuntimeError(f"demo API did not become ready: {last}")

    # -- HTTP helpers ------------------------------------------------------

    def _request(self, method: str, path: str, *, body: dict | None = None, headers: dict | None = None):
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
            hdrs.setdefault("Origin", self.origin)
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=RAW_TIMEOUT) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            return exc.code, parsed

    def create(self, body: dict):
        return self._request("POST", "/api/endpoint-profiles", body=body)

    def create_ok(self, index: int, **kwargs) -> dict:
        status, payload = self.create(profile_payload(index, **kwargs))
        self.assertEqual(status, 201, f"create should return 201, got {status} {payload}")
        return payload

    def get(self, path: str):
        return self._request("GET", path)

    def stderr_text(self) -> str:
        return self.err_path.read_text(encoding="utf-8", errors="replace")

    def raw_http(self, head_lines, body: bytes = b"") -> Response:
        head = ("\r\n".join(head_lines) + "\r\n\r\n").encode("latin-1")
        with socket.create_connection((self.host, self.port), timeout=RAW_TIMEOUT) as sock:
            sock.settimeout(RAW_TIMEOUT)
            sock.sendall(head + body)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            chunks = []
            try:
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except socket.timeout:
                pass
        return _parse_response(b"".join(chunks))

    def profile_count(self) -> int:
        _status, payload = self.get("/api/endpoint-profiles")
        return len(payload["endpoint_profiles"])


class CreateAndEvaluateTests(_ProfileServerCase):
    def test_ten_distinct_profiles_create_and_evaluate_independently(self) -> None:
        created = []
        for i, spec in enumerate(PROFILE_MATRIX, start=1):
            payload = self.create_ok(1000 + i, **spec)
            created.append((payload, spec))

        ids = [p["profile"]["id"] for p, _ in created]
        bics = [p["institution"]["bic"] for p, _ in created]
        leis = [p["legal_entity"]["lei"] for p, _ in created]
        uetrs = [p["endpoint"]["uetr"] for p, _ in created]
        self.assertEqual(len(set(ids)), 10, "profile ids must be distinct")
        self.assertEqual(len(set(bics)), 10, "profile BICs must be distinct")
        self.assertEqual(len(set(leis)), 10, "profile LEIs must be distinct")
        self.assertEqual(len(set(uetrs)), 10, "profile UETRs must be distinct")

        for payload, spec in created:
            verdict = payload["evaluation"]["decision"]["verdict"]
            self.assertEqual(
                verdict, spec["verdict"],
                f"profile {payload['profile']['id']} evaluated {verdict}, expected {spec['verdict']}",
            )
            self.assertEqual(payload["profile"]["lifecycle_state"], "draft")
            self.assertEqual(payload["profile"]["tenant_id"], "synthetic-demo")
            self.assertEqual(len(payload["evaluation"]["checks"]), 6)

        # Re-read the first profile through the collection API and prove its
        # verdict was not mutated by the nine later creates (no shared state).
        first_id = created[0][0]["profile"]["id"]
        _status, reread = self.get(f"/api/endpoint-profiles/{first_id}")
        self.assertEqual(reread["evaluation"]["decision"]["verdict"], APPROVED)

    def test_create_persists_parsed_source_lineage_on_every_constituent(self) -> None:
        payload = self.create_ok(2001)
        for section in ("institution", "legal_entity", "endpoint"):
            lineage = payload[section]["source_lineage"]
            self.assertIsInstance(lineage, dict, f"{section} source_lineage must be parsed JSON")
            self.assertEqual(lineage["kind"], "source_lineage")

    def test_list_reports_lifecycle_and_verdict(self) -> None:
        self.create_ok(2100, authority="current", allowlist="stale", payload="complete")
        _status, listing = self.get("/api/endpoint-profiles")
        self.assertIn("endpoint_profiles", listing)
        match = [row for row in listing["endpoint_profiles"] if row.get("uetr") == "SYN-PROFILE-2100"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["lifecycle_state"], "draft")
        self.assertEqual(match[0]["verdict"], BLOCKED)


class ValidationRejectionTests(_ProfileServerCase):
    def _assert_rejected_no_write(self, body, status, error, ctx) -> None:
        before = self.profile_count()
        got_status, payload = self.create(body)
        self.assertEqual(got_status, status, f"{ctx}: expected {status}, got {got_status} {payload}")
        self.assertIsInstance(payload, dict, f"{ctx}: expected JSON error body, got {payload}")
        self.assertEqual(payload.get("error"), error, f"{ctx}: expected {error}, got {payload}")
        self.assertEqual(self.profile_count(), before, f"{ctx}: rejected create must not write")

    def test_missing_section_rejected(self) -> None:
        body = profile_payload(3001)
        del body["fallback"]
        self._assert_rejected_no_write(body, 422, "missing_field", "missing_fallback_section")

    def test_unknown_top_level_key_rejected(self) -> None:
        body = profile_payload(3002)
        body["tenant_id"] = "attacker-tenant"
        self._assert_rejected_no_write(body, 422, "unknown_field", "client_supplied_tenant")

    def test_unknown_nested_key_rejected(self) -> None:
        body = profile_payload(3003)
        body["endpoint"]["source_lineage"] = {"kind": "spoofed"}
        self._assert_rejected_no_write(body, 422, "unknown_field", "client_supplied_lineage")

    def test_missing_required_field_rejected(self) -> None:
        body = profile_payload(3004)
        del body["institution"]["bic"]
        self._assert_rejected_no_write(body, 422, "missing_field", "missing_bic")

    def test_wrong_type_field_rejected(self) -> None:
        body = profile_payload(3005)
        body["institution"]["name"] = 123
        self._assert_rejected_no_write(body, 422, "invalid_field", "non_string_name")

    def test_empty_string_field_rejected(self) -> None:
        body = profile_payload(3006)
        body["endpoint"]["wallet_address"] = "   "
        self._assert_rejected_no_write(body, 422, "invalid_field", "blank_wallet")

    def test_overlong_field_rejected_as_field_not_size(self) -> None:
        body = profile_payload(3007)
        # Over the per-field length bound but well under the whole-body size
        # bound, so the create route rejects on the field-length rule (422 field),
        # not the framing size rule (413) -- and never a partial write.
        body["legal_entity"]["name"] = "A" * 300
        self._assert_rejected_no_write(body, 422, "invalid_field", "overlong_name")

    def test_bad_enum_rejected(self) -> None:
        for section, field, ctx in [
            ("legal_entity", "authority_status", "authority"),
            ("endpoint", "allowlist_status", "allowlist"),
            ("endpoint", "endpoint_payload_status", "payload"),
        ]:
            body = profile_payload(3008)
            body[section][field] = "definitely_not_supported"
            self._assert_rejected_no_write(body, 422, "unsupported_enum", f"bad_enum_{ctx}")

    def test_non_object_body_rejected(self) -> None:
        before = self.profile_count()
        resp = self.raw_http(
            [
                "POST /api/endpoint-profiles HTTP/1.1",
                f"Host: {self.host}:{self.port}",
                "Content-Type: application/json",
                f"Origin: {self.origin}",
                "Content-Length: 2",
                "Connection: close",
            ],
            b"[]",
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.json.get("error"), "invalid_payload")
        self.assertEqual(self.profile_count(), before)

    def test_duplicate_unique_values_rejected(self) -> None:
        base = profile_payload(3100)
        self.create_ok(3100)

        # An identical body collides on the first unique column (bic).
        self._assert_rejected_no_write(profile_payload(3100), 409, "duplicate_bic", "duplicate_all")

        dup_bic = profile_payload(3101)
        dup_bic["institution"]["bic"] = base["institution"]["bic"]
        self._assert_rejected_no_write(dup_bic, 409, "duplicate_bic", "duplicate_bic_only")

        dup_lei = profile_payload(3102)
        dup_lei["legal_entity"]["lei"] = base["legal_entity"]["lei"]
        self._assert_rejected_no_write(dup_lei, 409, "duplicate_lei", "duplicate_lei_only")

        dup_uetr = profile_payload(3103)
        dup_uetr["endpoint"]["uetr"] = base["endpoint"]["uetr"]
        self._assert_rejected_no_write(dup_uetr, 409, "duplicate_uetr", "duplicate_uetr_only")

    def test_duplicate_bic_against_seeded_institution_rejected(self) -> None:
        body = profile_payload(3200)
        body["institution"]["bic"] = "MERIDEFFXXX"  # a seeded institution BIC
        self._assert_rejected_no_write(body, 409, "duplicate_bic", "seeded_bic_collision")


class UpdateLifecycleTests(_ProfileServerCase):
    def test_update_allowed_in_draft_and_changes_evaluation(self) -> None:
        created = self.create_ok(4001, authority="current", allowlist="current", payload="complete")
        pid = created["profile"]["id"]
        self.assertEqual(created["evaluation"]["decision"]["verdict"], APPROVED)

        updated_body = profile_payload(4001, authority="current", allowlist="stale", payload="complete")
        status, payload = self._request("PUT", f"/api/endpoint-profiles/{pid}", body=updated_body)
        self.assertEqual(status, 200, f"draft update should succeed, got {status} {payload}")
        self.assertEqual(payload["profile"]["lifecycle_state"], "draft")
        self.assertEqual(payload["evaluation"]["decision"]["verdict"], BLOCKED)

    def test_update_rejected_after_activation(self) -> None:
        created = self.create_ok(4002)
        pid = created["profile"]["id"]
        status, _ = self._request("POST", f"/api/endpoint-profiles/{pid}/activation", body={})
        self.assertEqual(status, 200)

        before = self.get(f"/api/endpoint-profiles/{pid}")[1]
        status, payload = self._request("PUT", f"/api/endpoint-profiles/{pid}", body=profile_payload(4002))
        self.assertEqual(status, 409, f"active profile must be immutable, got {status} {payload}")
        self.assertEqual(payload.get("error"), "profile_not_draft")
        after = self.get(f"/api/endpoint-profiles/{pid}")[1]
        self.assertEqual(after["endpoint"]["uetr"], before["endpoint"]["uetr"], "no write on rejected update")


class ActivationSupersessionTests(_ProfileServerCase):
    def test_activate_draft_to_active(self) -> None:
        created = self.create_ok(5001)
        pid = created["profile"]["id"]
        status, payload = self._request("POST", f"/api/endpoint-profiles/{pid}/activation", body={})
        self.assertEqual(status, 200, f"activation should succeed, got {status} {payload}")
        self.assertEqual(payload["profile"]["lifecycle_state"], "active")

    def test_activate_non_draft_rejected(self) -> None:
        created = self.create_ok(5002)
        pid = created["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{pid}/activation", body={})
        status, payload = self._request("POST", f"/api/endpoint-profiles/{pid}/activation", body={})
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "invalid_transition")

    def test_supersede_is_atomic_replacement(self) -> None:
        active = self.create_ok(5003)
        active_id = active["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{active_id}/activation", body={})

        replacement = self.create_ok(5004)
        repl_id = replacement["profile"]["id"]

        status, payload = self._request(
            "POST", f"/api/endpoint-profiles/{active_id}/supersession", body={"replacement_id": repl_id}
        )
        self.assertEqual(status, 200, f"supersession should succeed, got {status} {payload}")

        _s, old = self.get(f"/api/endpoint-profiles/{active_id}")
        _s, new = self.get(f"/api/endpoint-profiles/{repl_id}")
        self.assertEqual(old["profile"]["lifecycle_state"], "superseded")
        self.assertEqual(old["profile"]["superseded_by"], repl_id)
        self.assertEqual(new["profile"]["lifecycle_state"], "active")

    def test_superseded_profile_still_readable_and_immutable(self) -> None:
        active = self.create_ok(5005)
        active_id = active["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{active_id}/activation", body={})
        replacement = self.create_ok(5006)
        repl_id = replacement["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{active_id}/supersession", body={"replacement_id": repl_id})

        status, payload = self.get(f"/api/endpoint-profiles/{active_id}")
        self.assertEqual(status, 200, "old profile must remain readable after supersession")
        self.assertEqual(payload["profile"]["lifecycle_state"], "superseded")
        # No transition out of superseded: activation and update both fail closed.
        act_status, _ = self._request("POST", f"/api/endpoint-profiles/{active_id}/activation", body={})
        self.assertEqual(act_status, 409)
        put_status, _ = self._request("PUT", f"/api/endpoint-profiles/{active_id}", body=profile_payload(5005))
        self.assertEqual(put_status, 409)

    def test_supersede_requires_active_source(self) -> None:
        draft = self.create_ok(5007)
        draft_id = draft["profile"]["id"]
        replacement = self.create_ok(5008)
        repl_id = replacement["profile"]["id"]
        # Source is still draft (not active) -> invalid transition.
        status, payload = self._request(
            "POST", f"/api/endpoint-profiles/{draft_id}/supersession", body={"replacement_id": repl_id}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "invalid_transition")

    def test_supersede_requires_draft_replacement(self) -> None:
        active = self.create_ok(5009)
        active_id = active["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{active_id}/activation", body={})
        other_active = self.create_ok(5010)
        other_id = other_active["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{other_id}/activation", body={})
        # Replacement is active, not draft -> invalid transition, no write.
        status, payload = self._request(
            "POST", f"/api/endpoint-profiles/{active_id}/supersession", body={"replacement_id": other_id}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "invalid_transition")
        _s, still = self.get(f"/api/endpoint-profiles/{active_id}")
        self.assertEqual(still["profile"]["lifecycle_state"], "active", "failed supersession must not write")

    def test_supersede_self_rejected(self) -> None:
        active = self.create_ok(5011)
        active_id = active["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{active_id}/activation", body={})
        status, payload = self._request(
            "POST", f"/api/endpoint-profiles/{active_id}/supersession", body={"replacement_id": active_id}
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload.get("error"), "invalid_replacement")

    def test_supersede_missing_replacement_rejected(self) -> None:
        active = self.create_ok(5012)
        active_id = active["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{active_id}/activation", body={})
        for body, ctx in [({}, "empty"), ({"replacement_id": "5"}, "string"), ({"replacement_id": -1}, "negative")]:
            status, payload = self._request(
                "POST", f"/api/endpoint-profiles/{active_id}/supersession", body=body
            )
            self.assertEqual(status, 422, ctx)
            self.assertEqual(payload.get("error"), "invalid_replacement", ctx)

    def test_supersede_absent_replacement_rejected(self) -> None:
        active = self.create_ok(5013)
        active_id = active["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{active_id}/activation", body={})
        status, payload = self._request(
            "POST", f"/api/endpoint-profiles/{active_id}/supersession", body={"replacement_id": 99999999}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("error"), "replacement_not_found")


class IdentifierAndRoutingTests(_ProfileServerCase):
    def test_strict_numeric_id_rejected(self) -> None:
        for bad in ("abc", "1.0", "01", "0", "-1", "%20", "1e3"):
            status, payload = self.get(f"/api/endpoint-profiles/{bad}")
            self.assertEqual(status, 400, f"malformed id {bad!r} must be 400, got {status} {payload}")
            self.assertEqual(payload.get("error"), "invalid_profile_id", bad)

    def test_absent_numeric_id_is_404(self) -> None:
        status, payload = self.get("/api/endpoint-profiles/98765432")
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("error"), "profile_not_found")

    def test_overlong_numeric_id_rejected_without_crash_or_write(self) -> None:
        # A multi-thousand-digit id must be a deterministic 400 (never a 500 from
        # int() exceeding CPython's digit cap) on both the read and write routes,
        # with no write, and the server must stay alive afterward.
        huge = "9" * 5000
        status, payload = self.get(f"/api/endpoint-profiles/{huge}")
        self.assertEqual(status, 400, f"overlong id must be 400, got {status} {payload}")
        self.assertEqual(payload.get("error"), "invalid_profile_id")

        before = self.profile_count()
        write_status, write_payload = self._request(
            "POST", f"/api/endpoint-profiles/{huge}/activation", body={}
        )
        self.assertEqual(write_status, 400, f"overlong id on a write route must be 400, got {write_status} {write_payload}")
        self.assertEqual(write_payload.get("error"), "invalid_profile_id")
        self.assertEqual(self.profile_count(), before, "an overlong id must not write")

        live_status, _ = self.get("/api/endpoint-profiles")
        self.assertEqual(live_status, 200, "server must stay alive after an oversized id")

    def test_no_delete_route(self) -> None:
        created = self.create_ok(6001)
        pid = created["profile"]["id"]
        before = self.profile_count()
        status, _ = self._request("DELETE", f"/api/endpoint-profiles/{pid}")
        self.assertIn(status, (404, 405), "there must be no delete route")
        # The profile still exists and is readable.
        get_status, _ = self.get(f"/api/endpoint-profiles/{pid}")
        self.assertEqual(get_status, 200)
        self.assertEqual(self.profile_count(), before)


class WriteRouteSecurityTests(_ProfileServerCase):
    def _create_headers(self, body: bytes, *, content_type="application/json", origin="__server__", content_length="__auto__"):
        lines = ["POST /api/endpoint-profiles HTTP/1.1", f"Host: {self.host}:{self.port}"]
        if content_type is not None:
            lines.append(f"Content-Type: {content_type}")
        if origin == "__server__":
            origin = self.origin
        if origin is not None:
            lines.append(f"Origin: {origin}")
        if content_length == "__auto__":
            content_length = str(len(body))
        if content_length is not None:
            lines.append(f"Content-Length: {content_length}")
        lines.append("Connection: close")
        return lines

    def test_create_requires_json_content_type(self) -> None:
        before = self.profile_count()
        body = json.dumps(profile_payload(7001)).encode("utf-8")
        resp = self.raw_http(self._create_headers(body, content_type="text/plain"), body)
        self.assertEqual(resp.status, 415)
        self.assertEqual(resp.json.get("error"), "unsupported_media_type")
        self.assertEqual(self.profile_count(), before)

    def test_create_rejects_cross_origin(self) -> None:
        before = self.profile_count()
        body = json.dumps(profile_payload(7002)).encode("utf-8")
        resp = self.raw_http(self._create_headers(body, origin="http://evil.example"), body)
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.json.get("error"), "origin_not_allowed")
        self.assertEqual(self.profile_count(), before)

    def test_create_requires_content_length(self) -> None:
        before = self.profile_count()
        body = json.dumps(profile_payload(7003)).encode("utf-8")
        resp = self.raw_http(self._create_headers(body, content_length=None), body)
        self.assertEqual(resp.status, 411)
        self.assertEqual(resp.json.get("error"), "length_required")
        self.assertEqual(self.profile_count(), before)

    def test_create_rejects_oversized_body(self) -> None:
        before = self.profile_count()
        body = ("9" * 9000).encode("utf-8")
        resp = self.raw_http(self._create_headers(body), body)
        self.assertEqual(resp.status, 413)
        self.assertEqual(resp.json.get("error"), "request_too_large")
        self.assertEqual(self.profile_count(), before)

    def test_profile_write_logs_collapsed_route_without_field_values(self) -> None:
        before_len = len(self.stderr_text())
        payload = self.create_ok(7100)
        pid = payload["profile"]["id"]
        new_text = self.stderr_text()[before_len:]
        records = [json.loads(line) for line in new_text.splitlines() if line.strip().startswith("{")]
        create_events = [
            r for r in records
            if r.get("event") == "request" and r.get("path") == "/api/endpoint-profiles" and r.get("status") == 201
        ]
        self.assertTrue(create_events, "a profile create must log the collapsed collection route")
        # The unique synthetic BIC/UETR must never appear in any log line.
        self.assertNotIn("SYNBIC7100XXX", new_text, "profile field values must never be logged")
        self.assertNotIn("SYN-PROFILE-7100", new_text)

        before_len = len(self.stderr_text())
        self._request("POST", f"/api/endpoint-profiles/{pid}/activation", body={})
        act_text = self.stderr_text()[before_len:]
        act_records = [json.loads(line) for line in act_text.splitlines() if line.strip().startswith("{")]
        self.assertTrue(
            any(
                r.get("event") == "request" and r.get("path") == "/api/endpoint-profiles/{id}/activation"
                for r in act_records
            ),
            "activation must log the id-collapsed transition route",
        )
        # The concrete id-bearing path must never appear; only the {id} label.
        self.assertNotIn(f"/api/endpoint-profiles/{pid}/activation", act_text)


class ScenarioReproductionTests(_ProfileServerCase):
    """Creating profiles must not disturb the three shipped scenarios."""

    EXPECTED = {
        "blocked": BLOCKED,
        "refreshed": APPROVED,
        "authority": AUTHORITY_HOLD,
    }

    def test_scenarios_still_reproduce_after_profile_writes(self) -> None:
        self.create_ok(8001)
        self.create_ok(8002, allowlist="stale")
        for slug, verdict in self.EXPECTED.items():
            status, payload = self.get(f"/api/scenarios/{slug}")
            self.assertEqual(status, 200)
            self.assertEqual(payload["decision"]["verdict"], verdict, f"{slug} verdict must be byte-for-byte stable")
            self.assertEqual(payload["payment_context"]["settlement_endpoint"]["id"], 1, "scenario still on endpoint 1")


class _LifecycleConcurrencyCase(unittest.TestCase):
    """Deterministic coordinated two-thread lifecycle races (SEC-P20).

    ``ThreadingHTTPServer`` serves each request on its own thread with its own
    sqlite connection, so two operator actions can execute the same lifecycle
    service function concurrently. These tests drive the real ``app`` service
    functions from two coordinated threads against a disposable temp database and
    reproduce the exact create/activate/supersede races -- no probabilistic stress
    loop. Coordination rides ``sqlite3`` statement tracing: a thread is paused at
    the instant it is about to issue its first write, after its precondition read,
    so both threads read stale state before either writes.
    """

    # The winner of a serialized write pauses at its write barrier waiting for the
    # loser, who is blocked acquiring the write lock and never arrives; the barrier
    # times out (well under the loser's busy timeout) and the winner proceeds. In
    # the pre-fix code both threads reach the barrier immediately (no lock is held
    # at read time), so the timeout never elapses.
    BARRIER_TIMEOUT = 1.5
    WAIT_TIMEOUT = 10.0
    INJECT_BUSY_TIMEOUT = 1.0
    INJECT_TIMEOUT = 10.0

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sec_p20_conc_")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "demo.sqlite"
        with mock.patch.object(seed, "DB_PATH", self.db_path):
            seed.seed()
        db_patch = mock.patch.object(app, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

    # -- helpers -----------------------------------------------------------

    def _create_draft(self, index: int, **kwargs) -> int:
        sections = app.validate_profile_payload(profile_payload(index, **kwargs))
        return app.create_profile(sections)["profile"]["id"]

    def _state(self, profile_id: int):
        return app.load_profile(profile_id)["profile"]

    def _coordinated_get_db(self, on_first_write):
        """Wrap ``app.get_db`` so each new connection pauses (once) the instant it
        issues its first write statement -- i.e. after the precondition read."""
        real_get_db = app.get_db

        def wrapped():
            con = real_get_db()
            fired = {"done": False}

            def tracer(statement: str) -> None:
                if not fired["done"] and statement.lstrip().upper().startswith("UPDATE "):
                    fired["done"] = True
                    on_first_write()

            con.set_trace_callback(tracer)
            return con

        return wrapped

    def _barrier_pause(self, barrier):
        def on_first_write():
            try:
                barrier.wait(timeout=self.BARRIER_TIMEOUT)
            except threading.BrokenBarrierError:
                pass
        return on_first_write

    @staticmethod
    def _run_threads(targets):
        threads = [threading.Thread(target=fn) for fn in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()


class ActivationRaceTests(_LifecycleConcurrencyCase):
    def test_two_concurrent_activations_yield_one_success_one_conflict(self) -> None:
        pid = self._create_draft(9101)
        barrier = threading.Barrier(2)
        results: dict[str, tuple] = {}

        def worker(name):
            def run():
                try:
                    app.activate_profile(pid)
                    results[name] = ("ok", None)
                except app.RequestRejected as exc:
                    results[name] = ("rejected", exc.error)
                except Exception as exc:  # noqa: BLE001 - surface unexpected errors
                    results[name] = ("error", f"{type(exc).__name__}: {exc}")
            return run

        with mock.patch.object(app, "get_db", self._coordinated_get_db(self._barrier_pause(barrier))):
            self._run_threads([worker("T1"), worker("T2")])

        outcomes = sorted(v[0] for v in results.values())
        self.assertEqual(
            outcomes, ["ok", "rejected"],
            f"two concurrent activations must yield exactly one success and one conflict, got {results}",
        )
        rejected = next(v for v in results.values() if v[0] == "rejected")
        self.assertEqual(rejected[1], "invalid_transition", f"the losing activation must be a stable 409, got {results}")
        # The DB linearized to active exactly once.
        self.assertEqual(self._state(pid)["lifecycle_state"], "active")


class SupersessionRaceTests(_LifecycleConcurrencyCase):
    def test_two_concurrent_supersessions_pick_one_active_replacement(self) -> None:
        active_id = self._create_draft(9201)
        app.activate_profile(active_id)
        r1 = self._create_draft(9202)
        r2 = self._create_draft(9203)
        barrier = threading.Barrier(2)
        results: dict[str, tuple] = {}

        def worker(name, replacement_id):
            def run():
                try:
                    app.supersede_profile(active_id, replacement_id)
                    results[name] = ("ok", None)
                except app.RequestRejected as exc:
                    results[name] = ("rejected", exc.error)
                except Exception as exc:  # noqa: BLE001 - surface unexpected errors
                    results[name] = ("error", f"{type(exc).__name__}: {exc}")
            return run

        with mock.patch.object(app, "get_db", self._coordinated_get_db(self._barrier_pause(barrier))):
            self._run_threads([worker("T1", r1), worker("T2", r2)])

        outcomes = sorted(v[0] for v in results.values())
        self.assertEqual(
            outcomes, ["ok", "rejected"],
            f"two concurrent supersessions of one active source must yield one success and one conflict, got {results}",
        )
        rejected = next(v for v in results.values() if v[0] == "rejected")
        self.assertEqual(rejected[1], "invalid_transition", f"the losing supersession must be a stable 409, got {results}")

        # Exactly one replacement becomes active; the losing replacement stays draft.
        r1_state = self._state(r1)["lifecycle_state"]
        r2_state = self._state(r2)["lifecycle_state"]
        self.assertEqual(
            sorted([r1_state, r2_state]), ["active", "draft"],
            f"exactly one replacement must be active and the loser remain draft, got r1={r1_state} r2={r2_state}",
        )
        # The source is superseded exactly once, linked to the chosen active replacement.
        source = self._state(active_id)
        self.assertEqual(source["lifecycle_state"], "superseded")
        chosen = r1 if r1_state == "active" else r2
        self.assertEqual(source["superseded_by"], chosen, "source must link to the single chosen replacement")


class DraftUpdateRaceTests(_LifecycleConcurrencyCase):
    def test_draft_update_and_activation_are_mutually_exclusive(self) -> None:
        # A draft update reads the draft precondition and then rewrites the
        # constituents. In the pre-fix code that read is outside any transaction,
        # so a competing activation can linearize (commit) in the window between
        # the update's read and its write -- and the update then mutates a profile
        # that is already active. This regression forces exactly that interleaving:
        # the update is paused the instant it is about to issue its first write
        # (its draft precondition already read), and a competing activation on an
        # independent connection tries to commit. It must NOT be able to.
        pid = self._create_draft(9301)
        updated_sections = app.validate_profile_payload(profile_payload(9301, allowlist="stale"))

        updater_reached_write = threading.Event()
        injection_done = threading.Event()
        state = {}

        def on_first_write():
            updater_reached_write.set()
            injection_done.wait(timeout=self.INJECT_TIMEOUT)

        def run_update():
            try:
                app.update_profile(pid, updated_sections)
                state["update"] = ("ok", None)
            except app.RequestRejected as exc:
                state["update"] = ("rejected", exc.error)
            except Exception as exc:  # noqa: BLE001 - surface unexpected errors
                state["update"] = ("error", f"{type(exc).__name__}: {exc}")

        with mock.patch.object(app, "get_db", self._coordinated_get_db(on_first_write)):
            updater = threading.Thread(target=run_update)
            updater.start()
            self.assertTrue(
                updater_reached_write.wait(timeout=self.WAIT_TIMEOUT),
                "the update never reached its write window",
            )
            # Competing activation on an independent connection, mid-update.
            competitor = sqlite3.connect(self.db_path, timeout=self.INJECT_BUSY_TIMEOUT)
            try:
                competitor.execute("BEGIN IMMEDIATE")
                competitor.execute(
                    "UPDATE endpoint_profiles SET lifecycle_state='active', updated_at='race-activation' WHERE id=?",
                    (pid,),
                )
                competitor.execute("COMMIT")
                state["activation_linearized"] = True
            except sqlite3.OperationalError:
                state["activation_linearized"] = False
            finally:
                competitor.close()
                injection_done.set()
            updater.join(timeout=self.WAIT_TIMEOUT)

        self.assertNotIn("error", [state.get("update", ("?",))[0]], f"unexpected update error: {state}")
        # The core guarantee: while a draft update holds its read->write window, a
        # competing activation cannot linearize. In the pre-fix code it does, which
        # is precisely the stale-read window that lets the update mutate an already
        # activated profile.
        self.assertFalse(
            state.get("activation_linearized"),
            "a competing activation linearized during an in-flight draft update (stale-read window is open)",
        )


class HealthTenantScopeTests(_LifecycleConcurrencyCase):
    def test_health_endpoint_profile_count_is_tenant_scoped(self) -> None:
        # A fresh seed has exactly one server-owned (synthetic-demo) profile. Inject
        # a foreign-tenant profile row directly (FK enforcement off) and require
        # health() to count ONLY the server-owned tenant -- every profile read is
        # server-side tenant scoped, health included.
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "INSERT INTO endpoint_profiles(tenant_id, endpoint_id, lifecycle_state, superseded_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                ("attacker-tenant", 2, "active", None, "t", "t"),
            )
            con.commit()
        finally:
            con.close()

        result = app.health()
        self.assertEqual(
            result["endpoint_profile_count"], 1,
            "health() must count only the server-owned tenant's profiles",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
