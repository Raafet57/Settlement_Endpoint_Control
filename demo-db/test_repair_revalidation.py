#!/usr/bin/env python3
"""SEC-P30 tests: versioned decisions, repair loop, and revalidation.

These pin the contract of the endpoint-profile repair workflow over the real
localhost API (``demo-db/app.py``) and the ``profile_decisions`` / ``repair_tasks``
schema. An **active** profile whose latest advisory decision needs repair can be
carried through open -> evidence refresh -> revalidation, producing a second,
superseding decision computed solely from the refreshed persisted evidence, while
the prior decision and its immutable evidence snapshot stay queryable.

The profile aggregate is never mutated (SEC-P20 immutability is preserved): the
refreshed evidence is persisted on the repair task and the revalidation decision
is an advisory overlay. Legal step order is open -> evidence -> revalidation;
every out-of-order, replayed, or cross-profile step fails closed transactionally.

All fixtures are synthetic, localhost-only, and make no external network calls.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import app
import migrate
import seed

ROOT = Path(__file__).resolve().parent
SEED = ROOT / "seed.py"
APP = ROOT / "app.py"
RAW_TIMEOUT = 4.0

APPROVED = "TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED"
BLOCKED = "TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED"
AUTHORITY_HOLD = "AUTHORITY_EXPIRED_MANUAL_HOLD"
INSUFFICIENT_HOLD = "INSUFFICIENT_INPUT_MANUAL_HOLD"


def profile_payload(index, *, authority="current", allowlist="current", payload="complete"):
    """A fully-formed, distinct synthetic profile body keyed by ``index``."""
    tag = f"{index:03d}"
    return {
        "institution": {
            "name": f"Synthetic Institution {tag}",
            "bic": f"RPRBIC{tag}XXX",
            "jurisdiction": "EU synthetic profile",
        },
        "legal_entity": {
            "name": f"Synthetic Entity {tag}",
            "lei": f"RPRLEI0000000{tag}",
            "authority_status": authority,
        },
        "endpoint": {
            "wallet_address": f"0xRPR{tag}",
            "custody": "Approved custodian",
            "allowlist_status": allowlist,
            "endpoint_owner": "Treasury ops queue",
            "endpoint_payload_status": payload,
            "requested_rail": "Tokenized deposit",
            "uetr": f"RPR-PROFILE-{tag}",
        },
        "fallback": {
            "fallback_rail": "Fiat SSI route",
            "fallback_currency": "EUR",
            "fallback_account_mask": "DE•• •••• •••• 4400",
            "fallback_intermediary_bic": "INTERDEFFXXX",
        },
    }


class _RepairServerCase(unittest.TestCase):
    """Reseed the synthetic DB and run the real app.py for repair-workflow tests."""

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

    # -- workflow helpers --------------------------------------------------

    def create(self, index, **kwargs) -> dict:
        status, payload = self._request("POST", "/api/endpoint-profiles", body=profile_payload(index, **kwargs))
        self.assertEqual(status, 201, f"create should 201, got {status} {payload}")
        return payload

    def activate(self, pid) -> dict:
        status, payload = self._request("POST", f"/api/endpoint-profiles/{pid}/activation", body={})
        self.assertEqual(status, 200, f"activation should 200, got {status} {payload}")
        return payload

    def active_needs_repair(self, index, **kwargs) -> int:
        """Create+activate a profile whose live verdict needs repair; return its id."""
        created = self.create(index, **kwargs)
        pid = created["profile"]["id"]
        self.assertNotEqual(created["evaluation"]["decision"]["verdict"], APPROVED)
        self.activate(pid)
        return pid

    def open_repair(self, pid, actor="ops_analyst"):
        return self._request("POST", f"/api/endpoint-profiles/{pid}/repair", body={"actor": actor})

    def refresh_evidence(self, pid, **evidence):
        return self._request("POST", f"/api/endpoint-profiles/{pid}/repair/evidence", body=evidence)

    def revalidate(self, pid):
        return self._request("POST", f"/api/endpoint-profiles/{pid}/repair/revalidation", body={})

    def read_profile(self, pid) -> dict:
        status, payload = self._request("GET", f"/api/endpoint-profiles/{pid}")
        self.assertEqual(status, 200, f"read should 200, got {status} {payload}")
        return payload


class RepairHappyPathTests(_RepairServerCase):
    def test_open_repair_records_baseline_decision(self) -> None:
        pid = self.active_needs_repair(3001, allowlist="stale")  # BLOCKED
        status, payload = self.open_repair(pid)
        self.assertEqual(status, 201, f"open repair should 201, got {status} {payload}")
        task = payload["repair_task"]
        self.assertEqual(task["state"], "open")
        self.assertEqual(task["profile_id"], pid)
        baseline = payload["decision"]
        self.assertEqual(baseline["version"], 1)
        self.assertEqual(baseline["origin"], "baseline")
        self.assertEqual(baseline["verdict"], BLOCKED)
        self.assertIsNone(baseline["previous_decision_id"])
        # The active profile itself is untouched.
        profile = self.read_profile(pid)
        self.assertEqual(profile["profile"]["lifecycle_state"], "active")

    def test_full_repair_loop_to_allow(self) -> None:
        pid = self.active_needs_repair(3002, allowlist="stale")  # BLOCKED
        self.open_repair(pid)
        rs, rp = self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.assertEqual(rs, 200, f"evidence refresh should 200, got {rs} {rp}")
        self.assertEqual(rp["repair_task"]["state"], "evidence_refreshed")

        vs, vp = self.revalidate(pid)
        self.assertEqual(vs, 200, f"revalidation should 200, got {vs} {vp}")
        superseding = vp["decision"]
        self.assertEqual(superseding["version"], 2)
        self.assertEqual(superseding["origin"], "revalidation")
        self.assertEqual(superseding["verdict"], APPROVED, "repaired evidence must produce ALLOW")
        self.assertEqual(vp["repair_task"]["state"], "resolved")
        # The superseding decision links back to the repaired baseline.
        self.assertEqual(superseding["previous_decision_id"], vp["decisions"][0]["id"])

    def test_repair_can_yield_hold_from_new_values(self) -> None:
        # A repair that supplies still-insufficient evidence produces a HOLD, not
        # an ALLOW -- the second decision follows solely from the new values.
        pid = self.active_needs_repair(3003, allowlist="stale")  # BLOCKED
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="expired", allowlist_status="current", endpoint_payload_status="complete")
        vs, vp = self.revalidate(pid)
        self.assertEqual(vs, 200, f"revalidation should 200, got {vs} {vp}")
        self.assertEqual(vp["decision"]["verdict"], AUTHORITY_HOLD)

    def test_prior_decision_and_evidence_remain_queryable(self) -> None:
        pid = self.active_needs_repair(3004, allowlist="stale", payload="incomplete")  # BLOCKED
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)

        profile = self.read_profile(pid)
        decisions = profile["decisions"]
        self.assertEqual([d["version"] for d in decisions], [1, 2])
        prior = decisions[0]
        # The prior decision and its immutable evidence snapshot are intact.
        self.assertEqual(prior["verdict"], BLOCKED)
        self.assertEqual(prior["evidence"], {
            "authority_status": "current", "allowlist_status": "stale", "endpoint_payload_status": "incomplete",
        })
        self.assertEqual(decisions[1]["verdict"], APPROVED)
        self.assertEqual(decisions[1]["evidence"], {
            "authority_status": "current", "allowlist_status": "current", "endpoint_payload_status": "complete",
        })

    def test_repair_does_not_mutate_the_immutable_profile(self) -> None:
        # SEC-P20 immutability preserved: the active profile's constituents and its
        # intrinsic evaluation never change; the repair is an advisory overlay.
        pid = self.active_needs_repair(3005, allowlist="stale")  # BLOCKED
        before = self.read_profile(pid)
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)
        after = self.read_profile(pid)
        self.assertEqual(after["endpoint"]["allowlist_status"], "stale", "constituent evidence must be unchanged")
        self.assertEqual(after["endpoint"], before["endpoint"])
        self.assertEqual(after["evaluation"]["decision"]["verdict"], BLOCKED, "intrinsic evaluation unchanged")
        self.assertEqual(after["decisions"][-1]["verdict"], APPROVED, "latest advisory decision is repaired")

    def test_repair_sequence_is_internally_consistent(self) -> None:
        pid = self.active_needs_repair(3006, allowlist="stale")
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)
        profile = self.read_profile(pid)
        task, decisions = profile["repair"], profile["decisions"]
        v1, v2 = decisions[0], decisions[1]
        self.assertEqual(task["state"], "resolved")
        self.assertEqual(task["opened_decision_id"], v1["id"])
        self.assertEqual(task["resolved_decision_id"], v2["id"])
        self.assertEqual(v2["previous_decision_id"], v1["id"])
        # The recorded action/evidence/decision timestamps are ordered.
        self.assertLessEqual(task["created_at"], task["evidence_refreshed_at"])
        self.assertLessEqual(task["evidence_refreshed_at"], task["resolved_at"])
        self.assertEqual(v2["created_at"], task["resolved_at"])

    def test_repair_loop_iterates_until_allowed(self) -> None:
        # A revalidation that still holds can itself be repaired again: a genuine
        # loop that grows the immutable decision chain (v1 -> v2 -> v3).
        pid = self.active_needs_repair(3007, allowlist="stale")  # BLOCKED
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="expired", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)  # v2 AUTHORITY_HOLD
        # Second cycle supersedes v2.
        os_, op = self.open_repair(pid)
        self.assertEqual(os_, 201, f"second open should 201, got {os_} {op}")
        self.assertEqual(op["decision"]["version"], 2, "the still-failing v2 is the repaired baseline")
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        _s, vp = self.revalidate(pid)
        v3 = vp["decision"]
        self.assertEqual(v3["version"], 3)
        self.assertEqual(v3["verdict"], APPROVED)
        self.assertEqual([d["version"] for d in vp["decisions"]], [1, 2, 3])

    def test_reads_order_cycles_logically_when_primary_keys_are_reversed(self) -> None:
        # Primary keys are opaque identifiers, not chronology. Build two legal
        # cycles, then reverse task/event id order in a synthetic corruption seam.
        # Current repair and event history must still follow decision version and
        # per-task sequence rather than integer id order.
        pid = self.active_needs_repair(3008, allowlist="stale")
        self.open_repair(pid)
        self.refresh_evidence(
            pid, authority_status="expired", allowlist_status="current",
            endpoint_payload_status="complete",
        )
        self.revalidate(pid)  # v2 HOLD
        self.open_repair(pid)
        self.refresh_evidence(
            pid, authority_status="current", allowlist_status="current",
            endpoint_payload_status="complete",
        )
        self.revalidate(pid)  # v3 APPROVED

        con = sqlite3.connect(app.DB_PATH)
        try:
            con.execute("PRAGMA foreign_keys=OFF")
            tasks = con.execute(
                "SELECT rt.id FROM repair_tasks rt "
                "JOIN profile_decisions d ON d.id=rt.opened_decision_id "
                "WHERE rt.profile_id=? ORDER BY d.version", (pid,),
            ).fetchall()
            self.assertEqual(len(tasks), 2)
            first_id, second_id = tasks[0][0], tasks[1][0]
            task_base = con.execute("SELECT COALESCE(MAX(id),0) FROM repair_tasks").fetchone()[0] + 10
            first_tmp, second_tmp = -task_base - 1, -task_base - 2
            con.execute("UPDATE repair_events SET task_id=? WHERE task_id=?", (first_tmp, first_id))
            con.execute("UPDATE repair_tasks SET id=? WHERE id=?", (first_tmp, first_id))
            con.execute("UPDATE repair_events SET task_id=? WHERE task_id=?", (second_tmp, second_id))
            con.execute("UPDATE repair_tasks SET id=? WHERE id=?", (second_tmp, second_id))
            # Earlier cycle receives the larger id; latest cycle receives smaller.
            con.execute("UPDATE repair_tasks SET id=? WHERE id=?", (task_base + 2, first_tmp))
            con.execute("UPDATE repair_events SET task_id=? WHERE task_id=?", (task_base + 2, first_tmp))
            con.execute("UPDATE repair_tasks SET id=? WHERE id=?", (task_base + 1, second_tmp))
            con.execute("UPDATE repair_events SET task_id=? WHERE task_id=?", (task_base + 1, second_tmp))

            event_rows = con.execute(
                "SELECT id FROM repair_events WHERE profile_id=?", (pid,)
            ).fetchall()
            event_base = con.execute("SELECT COALESCE(MAX(id),0) FROM repair_events").fetchone()[0] + 10
            for (event_id,) in event_rows:
                con.execute("UPDATE repair_events SET id=? WHERE id=?", (-event_base - event_id, event_id))
            # Latest cycle receives lower event ids than the earlier cycle.
            for cycle_offset, task_id in enumerate((task_base + 1, task_base + 2)):
                rows = con.execute(
                    "SELECT id, sequence FROM repair_events WHERE task_id=? ORDER BY sequence", (task_id,)
                ).fetchall()
                for event_id, sequence in rows:
                    con.execute(
                        "UPDATE repair_events SET id=? WHERE id=?",
                        (event_base + cycle_offset * 3 + sequence, event_id),
                    )
            con.commit()
        finally:
            con.close()

        self.assertEqual(migrate.migrate(app.DB_PATH)["status"], "already_current")
        profile = self.read_profile(pid)
        decisions = profile["decisions"]
        self.assertEqual(profile["repair"]["opened_decision_id"], decisions[1]["id"])
        self.assertEqual(profile["repair"]["resolved_decision_id"], decisions[2]["id"])
        self.assertEqual([e["sequence"] for e in profile["repair_events"]], [1, 2, 3, 1, 2, 3])
        self.assertEqual(
            [e["task_id"] for e in profile["repair_events"]],
            [task_base + 2] * 3 + [task_base + 1] * 3,
        )


class RepairInvalidTransitionTests(_RepairServerCase):
    def test_open_on_draft_rejected(self) -> None:
        created = self.create(3101, allowlist="stale")
        pid = created["profile"]["id"]  # still a draft
        status, payload = self.open_repair(pid)
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "invalid_transition")

    def test_open_on_superseded_rejected(self) -> None:
        active = self.active_needs_repair(3102, allowlist="stale")
        replacement = self.create(3103)["profile"]["id"]
        self._request("POST", f"/api/endpoint-profiles/{active}/supersession", body={"replacement_id": replacement})
        status, payload = self.open_repair(active)
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "invalid_transition")

    def test_open_on_approved_profile_rejected(self) -> None:
        pid = self.create(3104)["profile"]["id"]  # APPROVED shape
        self.activate(pid)
        status, payload = self.open_repair(pid)
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "nothing_to_repair")

    def test_open_twice_rejected_as_replay(self) -> None:
        pid = self.active_needs_repair(3105, allowlist="stale")
        self.open_repair(pid)
        status, payload = self.open_repair(pid)
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "repair_already_open")

    def test_evidence_before_open_rejected(self) -> None:
        pid = self.active_needs_repair(3106, allowlist="stale")
        status, payload = self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "no_open_repair")

    def test_evidence_replayed_rejected(self) -> None:
        pid = self.active_needs_repair(3107, allowlist="stale")
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        status, payload = self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "invalid_repair_state")

    def test_revalidation_before_evidence_rejected(self) -> None:
        pid = self.active_needs_repair(3108, allowlist="stale")
        self.open_repair(pid)
        status, payload = self.revalidate(pid)
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "invalid_repair_state")

    def test_revalidation_replayed_rejected(self) -> None:
        pid = self.active_needs_repair(3109, allowlist="stale")
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)
        status, payload = self.revalidate(pid)
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "no_open_repair")

    def test_bad_evidence_enum_rejected_without_write(self) -> None:
        pid = self.active_needs_repair(3110, allowlist="stale")
        self.open_repair(pid)
        status, payload = self.refresh_evidence(pid, authority_status="bogus", allowlist_status="current", endpoint_payload_status="complete")
        self.assertEqual(status, 422)
        self.assertEqual(payload.get("error"), "unsupported_enum")
        # The task is untouched: it is still open and can still be refreshed.
        again, _ = self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.assertEqual(again, 200)

    def test_unknown_and_missing_evidence_fields_rejected(self) -> None:
        pid = self.active_needs_repair(3111, allowlist="stale")
        self.open_repair(pid)
        s1, p1 = self._request("POST", f"/api/endpoint-profiles/{pid}/repair/evidence",
                               body={"authority_status": "current", "allowlist_status": "current", "endpoint_payload_status": "complete", "extra": "x"})
        self.assertEqual(s1, 422)
        self.assertEqual(p1.get("error"), "unknown_field")
        s2, p2 = self._request("POST", f"/api/endpoint-profiles/{pid}/repair/evidence", body={"authority_status": "current"})
        self.assertEqual(s2, 422)
        self.assertEqual(p2.get("error"), "missing_field")

    def test_open_bad_actor_rejected(self) -> None:
        pid = self.active_needs_repair(3112, allowlist="stale")
        status, payload = self._request("POST", f"/api/endpoint-profiles/{pid}/repair", body={"actor": "intruder"})
        self.assertEqual(status, 422)
        self.assertEqual(payload.get("error"), "invalid_actor")

    def test_open_absent_profile_is_404(self) -> None:
        status, payload = self.open_repair(98765432)
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("error"), "profile_not_found")

    def test_repair_bad_id_is_400(self) -> None:
        status, payload = self._request("POST", "/api/endpoint-profiles/01/repair", body={"actor": "ops_analyst"})
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("error"), "invalid_profile_id")


class RepairCrossProfileTests(_RepairServerCase):
    def test_steps_are_strictly_profile_scoped(self) -> None:
        a = self.active_needs_repair(3201, allowlist="stale")
        b = self.active_needs_repair(3202, allowlist="stale")
        self.open_repair(a)
        self.refresh_evidence(a, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        # B has no repair task; advancing B must not borrow A's task.
        vs, vp = self.revalidate(b)
        self.assertEqual(vs, 409)
        self.assertEqual(vp.get("error"), "no_open_repair")
        rs, rp = self.refresh_evidence(b, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.assertEqual(rs, 409)
        self.assertEqual(rp.get("error"), "no_open_repair")
        # A is untouched by the cross-profile attempts.
        self.assertEqual(self.read_profile(a)["repair"]["state"], "evidence_refreshed")
        self.assertIsNone(self.read_profile(b)["repair"])


class RepairWriteSecurityTests(_RepairServerCase):
    def test_open_requires_json_content_type(self) -> None:
        status, _ = self._request("POST", "/api/endpoint-profiles/1/repair",
                                  body={"actor": "ops_analyst"}, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_open_rejects_cross_origin(self) -> None:
        status, payload = self._request("POST", "/api/endpoint-profiles/1/repair",
                                       body={"actor": "ops_analyst"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "origin_not_allowed")


class RepairEventLedgerTests(_RepairServerCase):
    """SEC-P30: an append-only profile-repair event trail records exactly one
    ordered event per state step (open -> evidence refresh -> revalidation),
    profile- and task-scoped, so the workflow leaves an auditable sequence the
    mutable repair_tasks row cannot itself provide. No request/response payload is
    stored -- only the step kind, the accountable actor, and the decision each step
    concerns.
    """

    def test_full_loop_appends_ordered_event_trail(self) -> None:
        pid = self.active_needs_repair(3301, allowlist="stale")  # BLOCKED
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)
        profile = self.read_profile(pid)
        events = profile["repair_events"]
        self.assertEqual(
            [e["event_type"] for e in events],
            ["repair_opened", "evidence_refreshed", "revalidated"],
        )
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3])
        task_id = profile["repair"]["id"]
        self.assertTrue(all(e["task_id"] == task_id for e in events))
        self.assertTrue(all(e["profile_id"] == pid for e in events))
        self.assertTrue(all(e["actor"] == "ops_analyst" for e in events))
        # Open + revalidation reference the decision they concern; evidence does not.
        decisions = profile["decisions"]
        self.assertEqual(events[0]["decision_id"], decisions[0]["id"])
        self.assertIsNone(events[1]["decision_id"])
        self.assertEqual(events[2]["decision_id"], decisions[1]["id"])
        # Ordered, non-decreasing timestamps that agree with the recorded task.
        self.assertLessEqual(events[0]["created_at"], events[1]["created_at"])
        self.assertLessEqual(events[1]["created_at"], events[2]["created_at"])
        self.assertEqual(events[0]["created_at"], profile["repair"]["created_at"])
        self.assertEqual(events[2]["created_at"], profile["repair"]["resolved_at"])

    def test_open_appends_exactly_one_event_in_response(self) -> None:
        pid = self.active_needs_repair(3302, allowlist="stale")
        _s, op = self.open_repair(pid)
        # The step response itself surfaces the freshly appended trail.
        self.assertEqual([e["event_type"] for e in op["repair_events"]], ["repair_opened"])
        self.assertEqual(op["repair_events"][0]["sequence"], 1)
        self.assertEqual(len(self.read_profile(pid)["repair_events"]), 1)

    def test_event_trail_spans_repair_cycles(self) -> None:
        # A second repair cycle appends a fresh ordered triple under a new task id.
        pid = self.active_needs_repair(3303, allowlist="stale")
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="expired", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)  # v2 HOLD
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)  # v3 ALLOW
        events = self.read_profile(pid)["repair_events"]
        self.assertEqual(
            [e["event_type"] for e in events],
            ["repair_opened", "evidence_refreshed", "revalidated"] * 2,
        )
        self.assertEqual([e["sequence"] for e in events], [1, 2, 3, 1, 2, 3])
        self.assertEqual(len({e["task_id"] for e in events}), 2, "one task per cycle")

    def test_no_repair_yields_empty_trail(self) -> None:
        pid = self.active_needs_repair(3304, allowlist="stale")
        self.assertEqual(self.read_profile(pid)["repair_events"], [])


class RepairLifecycleInterleavingTests(_RepairServerCase):
    """SEC-P30: it must be impossible to both supersede a profile and leave or
    advance a live repair on it. Supersession is rejected while a non-resolved task
    exists, and refresh/revalidate re-check active state inside their own
    transactions; the two directions cannot interleave to strand a live repair on a
    superseded profile.
    """

    def _draft_replacement(self):
        return self.create(3410)["profile"]["id"]

    def test_supersede_rejected_while_repair_open(self) -> None:
        pid = self.active_needs_repair(3401, allowlist="stale")
        self.open_repair(pid)
        replacement = self.create(3402)["profile"]["id"]
        status, payload = self._request(
            "POST", f"/api/endpoint-profiles/{pid}/supersession", body={"replacement_id": replacement}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "repair_in_progress")
        # The profile stays active and the live repair is intact.
        self.assertEqual(self.read_profile(pid)["profile"]["lifecycle_state"], "active")
        self.assertEqual(self.read_profile(pid)["repair"]["state"], "open")

    def test_supersede_rejected_while_evidence_refreshed(self) -> None:
        pid = self.active_needs_repair(3403, allowlist="stale")
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        replacement = self.create(3404)["profile"]["id"]
        status, payload = self._request(
            "POST", f"/api/endpoint-profiles/{pid}/supersession", body={"replacement_id": replacement}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "repair_in_progress")

    def test_supersede_allowed_after_repair_resolved(self) -> None:
        # A fully resolved repair leaves no live task, so supersession is permitted
        # and the resolved history stays queryable on the superseded profile.
        pid = self.active_needs_repair(3405, allowlist="stale")
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)
        replacement = self.create(3406)["profile"]["id"]
        status, _ = self._request(
            "POST", f"/api/endpoint-profiles/{pid}/supersession", body={"replacement_id": replacement}
        )
        self.assertEqual(status, 200)
        old = self.read_profile(pid)
        self.assertEqual(old["profile"]["lifecycle_state"], "superseded")
        self.assertEqual([d["version"] for d in old["decisions"]], [1, 2], "resolved history stays queryable")

    def test_concurrent_open_vs_supersession_cannot_strand_live_repair(self) -> None:
        # A controlled concurrent race between opening a repair and superseding the
        # same active profile. BEGIN IMMEDIATE serializes them, so exactly one wins;
        # the invariant to prove is that the end state is NEVER a superseded profile
        # that still carries a non-resolved repair.
        import threading

        pid = self.active_needs_repair(3407, allowlist="stale")
        replacement = self.create(3408)["profile"]["id"]
        barrier = threading.Barrier(2)
        results: dict[str, tuple] = {}

        def do_open():
            barrier.wait()
            results["open"] = self.open_repair(pid)

        def do_supersede():
            barrier.wait()
            results["supersede"] = self._request(
                "POST", f"/api/endpoint-profiles/{pid}/supersession", body={"replacement_id": replacement}
            )

        threads = [threading.Thread(target=do_open), threading.Thread(target=do_supersede)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        profile = self.read_profile(pid)
        state = profile["profile"]["lifecycle_state"]
        repair = profile["repair"]
        live = repair is not None and repair["state"] != "resolved"
        # The forbidden end state is superseded-with-a-live-repair.
        self.assertFalse(state == "superseded" and live, f"stranded live repair on superseded profile: {profile}")
        # Exactly one of the two operations succeeded against the active profile.
        open_status = results["open"][0]
        supersede_status = results["supersede"][0]
        if supersede_status == 200:
            self.assertEqual(state, "superseded")
            self.assertNotEqual(open_status, 201, "open must not have created a live repair on a superseded profile")
        else:
            self.assertEqual(state, "active")
            self.assertEqual(supersede_status, 409)


class AdvisoryVerdictPromotionTests(_RepairServerCase):
    """SEC-P30: after a repair changes the advisory verdict, the read surfaces
    promote the latest versioned decision while the intrinsic evaluation (computed
    from the immutable constituent fields) stays separately available and unchanged.
    """

    def _list(self):
        status, payload = self._request("GET", "/api/endpoint-profiles")
        self.assertEqual(status, 200)
        return {item["id"]: item for item in payload["endpoint_profiles"]}

    def test_collection_and_detail_promote_latest_after_revalidation(self) -> None:
        pid = self.active_needs_repair(3501, allowlist="stale")  # intrinsic BLOCKED
        # Before any decision the collection reports the intrinsic verdict and the
        # detail exposes no advisory decision yet.
        self.assertEqual(self._list()[pid]["verdict"], BLOCKED)
        before = self.read_profile(pid)
        self.assertIsNone(before["latest_decision"])
        self.assertEqual(before["verdict"], BLOCKED)

        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="current", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)  # HOLD/BLOCKED -> ALLOW

        # The collection verdict is now promoted from the latest versioned decision.
        self.assertEqual(self._list()[pid]["verdict"], APPROVED)
        after = self.read_profile(pid)
        # The detail exposes the explicit latest advisory decision/result...
        self.assertEqual(after["latest_decision"]["verdict"], APPROVED)
        self.assertEqual(after["verdict"], APPROVED)
        self.assertEqual(after["latest_decision"]["id"], after["decisions"][-1]["id"])
        # ...while the intrinsic evaluation still reflects the immutable fields.
        self.assertEqual(after["evaluation"]["decision"]["verdict"], BLOCKED)
        self.assertEqual(after["endpoint"]["allowlist_status"], "stale")

    def test_promoted_verdict_can_be_a_new_hold(self) -> None:
        # Promotion tracks the latest decision even when it is another HOLD, not ALLOW.
        pid = self.active_needs_repair(3502, allowlist="stale")
        self.open_repair(pid)
        self.refresh_evidence(pid, authority_status="expired", allowlist_status="current", endpoint_payload_status="complete")
        self.revalidate(pid)  # -> AUTHORITY_HOLD
        self.assertEqual(self._list()[pid]["verdict"], AUTHORITY_HOLD)
        self.assertEqual(self.read_profile(pid)["verdict"], AUTHORITY_HOLD)


class RepairReceiptSnapshotTests(unittest.TestCase):
    """SEC-P30: mutation receipts and compound read models are concurrency-consistent.

    Each mutation assembles its receipt from the exact task/decision ids on the
    mutating connection, inside its own transaction, so a concurrent next repair
    cycle cannot make a receipt report a later generation's task or a null/wrong
    decision. These run in-process against the seeded synthetic DB so the exact-id
    contract can be exercised deterministically without flaky timing.
    """

    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run([sys.executable, str(SEED)], check=True, text=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run([sys.executable, str(SEED)], check=True, text=True, capture_output=True)

    def _active_blocked(self, index) -> int:
        sections = app.validate_profile_payload(profile_payload(index, allowlist="stale"))
        created = app.create_profile(sections)
        pid = created["profile"]["id"]
        app.activate_profile(pid)
        return pid

    def test_revalidation_receipt_selects_exact_generation_not_newest(self) -> None:
        # Regression: the previous receipt reselected "the newest task" after commit,
        # so a concurrent next-cycle open could make a revalidation receipt report the
        # newer open task and a null/wrong decision. Refreshed evidence that still
        # holds (a HOLD result) is used so a second cycle could legitimately open.
        pid = self._active_blocked(3601)
        app.open_repair_task(pid, "ops_analyst")
        app.refresh_repair_evidence(pid, {"authority_status": "expired", "allowlist_status": "current", "endpoint_payload_status": "complete"})

        # Force the OLD post-commit "newest task" lookup to point at a DIFFERENT,
        # newer generation. The refactored receipt is built from exact ids inside the
        # transaction, so it must not consult _load_current_repair at all.
        sentinel = {"id": 999999, "profile_id": pid, "state": "open",
                    "opened_decision_id": 424242, "resolved_decision_id": None}
        with mock.patch.object(app, "_load_current_repair", return_value=sentinel):
            result = app.revalidate_repair(pid)

        task = result["repair_task"]
        self.assertEqual(task["state"], "resolved", "receipt must report the just-resolved task")
        self.assertNotEqual(task["id"], sentinel["id"], "receipt must not report the newer generation")
        self.assertIsNotNone(result["decision"], "receipt decision must not be null")
        self.assertEqual(result["decision"]["id"], task["resolved_decision_id"])
        self.assertEqual(result["decision"]["origin"], "revalidation")
        self.assertEqual(result["decision"]["verdict"], AUTHORITY_HOLD)

    def test_load_profile_read_model_is_internally_consistent(self) -> None:
        # The compound read model is one snapshot: the promoted verdict, latest
        # advisory decision, decision chain, and repair task all agree.
        pid = self._active_blocked(3602)
        app.open_repair_task(pid, "ops_analyst")
        app.refresh_repair_evidence(pid, {"authority_status": "current", "allowlist_status": "current", "endpoint_payload_status": "complete"})
        app.revalidate_repair(pid)

        prof = app.load_profile(pid)
        self.assertEqual(prof["latest_decision"]["id"], prof["decisions"][-1]["id"])
        self.assertEqual(prof["repair"]["resolved_decision_id"], prof["latest_decision"]["id"])
        self.assertEqual(prof["verdict"], prof["latest_decision"]["verdict"])
        self.assertEqual(prof["verdict"], APPROVED)
        # The intrinsic evaluation stays separately available and unchanged.
        self.assertEqual(prof["evaluation"]["decision"]["verdict"], BLOCKED)


class RepairNormalizedRouteTests(unittest.TestCase):
    """The logged path label collapses profile ids on the repair routes too."""

    def test_repair_routes_collapse_id(self) -> None:
        self.assertEqual(app.normalized_route("/api/endpoint-profiles/7/repair"), "/api/endpoint-profiles/{id}/repair")
        self.assertEqual(app.normalized_route("/api/endpoint-profiles/7/repair/evidence"), "/api/endpoint-profiles/{id}/repair/evidence")
        self.assertEqual(app.normalized_route("/api/endpoint-profiles/7/repair/revalidation"), "/api/endpoint-profiles/{id}/repair/revalidation")

    def test_repair_id_and_query_never_leak(self) -> None:
        label = app.normalized_route("/api/endpoint-profiles/999/repair/evidence?token=SENTINEL")
        self.assertEqual(label, "/api/endpoint-profiles/{id}/repair/evidence")
        self.assertNotIn("SENTINEL", label)
        self.assertNotIn("999", label)


class RepairTimestampOrderingTests(unittest.TestCase):
    """SEC-P30 (blocker B): each repair mutator must read its step timestamp only
    AFTER BEGIN IMMEDIATE has taken the write lock. Otherwise, under lock
    contention, a later-committing step could persist an earlier timestamp than an
    already-committed one, inverting the recorded action/evidence/decision order.

    This is proven behaviorally against a disposable, freshly seeded synthetic
    SQLite database with a narrow connection proxy that records exactly when
    ``BEGIN IMMEDIATE`` executes and a ``utc_now`` spy that records exactly when the
    step timestamp is taken -- no source-text assertions and no timing sleeps. The
    backfilled active profile 1 has a non-approved intrinsic verdict, so its
    open -> evidence -> revalidation cycle exercises all three mutators in order.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="sec_p30_ts_")
        cls.db_path = Path(cls._tmp.name) / "demo.sqlite"
        with mock.patch.object(seed, "DB_PATH", cls.db_path):
            seed.seed()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _run_traced(self, fn):
        """Run ``fn`` with ``app.get_db`` proxied and ``app.utc_now`` spied, sharing
        one ordered event list. The proxy records the instant BEGIN IMMEDIATE is
        executed on the write connection; the spy records the instant the step
        timestamp is read. Returns the event list in observed order."""
        events: list[str] = []
        real_get_db = app.get_db
        real_utc_now = app.utc_now

        class _OrderRecordingConnection:
            # Faithful, transparent wrapper: it records only BEGIN IMMEDIATE and
            # otherwise delegates execute/context-manager/attribute access to the
            # real connection, so the mutation still fully persists or rolls back.
            def __init__(self, con):
                self._con = con

            def execute(self, sql, *args):
                if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                    events.append("begin_immediate")
                return self._con.execute(sql, *args)

            def __enter__(self):
                self._con.__enter__()
                return self

            def __exit__(self, *exc):
                return self._con.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self._con, name)

        def proxied_get_db():
            return _OrderRecordingConnection(real_get_db())

        def spy_utc_now():
            events.append("utc_now")
            return real_utc_now()

        with mock.patch.object(app, "DB_PATH", self.db_path), \
                mock.patch.object(app, "get_db", proxied_get_db), \
                mock.patch.object(app, "utc_now", spy_utc_now):
            fn()
        return events

    def _assert_utc_after_begin(self, events, label) -> None:
        self.assertIn("begin_immediate", events, f"{label}: BEGIN IMMEDIATE was not observed")
        self.assertIn("utc_now", events, f"{label}: utc_now was not observed")
        self.assertLess(
            events.index("begin_immediate"), events.index("utc_now"),
            f"{label}: the step timestamp must be read AFTER BEGIN IMMEDIATE; order={events}",
        )

    def test_all_three_repair_mutators_read_timestamp_after_begin_immediate(self) -> None:
        # Reseed to a clean state so profile 1 carries no prior repair, then drive
        # the full open -> evidence -> revalidation cycle, tracing each mutator.
        with mock.patch.object(seed, "DB_PATH", self.db_path):
            seed.seed()

        open_events = self._run_traced(lambda: app.open_repair_task(1, "ops_analyst"))
        self._assert_utc_after_begin(open_events, "open_repair_task")

        refresh_events = self._run_traced(lambda: app.refresh_repair_evidence(
            1, {"authority_status": "current", "allowlist_status": "current", "endpoint_payload_status": "complete"}
        ))
        self._assert_utc_after_begin(refresh_events, "refresh_repair_evidence")

        reval_events = self._run_traced(lambda: app.revalidate_repair(1))
        self._assert_utc_after_begin(reval_events, "revalidate_repair")


if __name__ == "__main__":
    unittest.main(verbosity=2)
