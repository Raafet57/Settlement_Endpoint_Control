#!/usr/bin/env python3
"""Focused tests for the SEC-P10 deterministic route evaluator.

Every case drives evaluate() from synthetic field inputs only. No test may
pass a scenario slug, and the evaluator must never approve on unknown,
unsupported, or insufficient input.
"""
from __future__ import annotations

import unittest

from evaluator import evaluate

APPROVED = "TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED"
BLOCKED = "TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED"
AUTHORITY_HOLD = "AUTHORITY_EXPIRED_MANUAL_HOLD"
INSUFFICIENT_HOLD = "INSUFFICIENT_INPUT_MANUAL_HOLD"

EXPECTED_NAMES = [
    "BIC and institution shape",
    "LEI and counterparty profile",
    "vLEI-style authority evidence",
    "Wallet allowlist freshness",
    "Endpoint-control payload",
    "Fiat SSI fallback",
]


def current_complete_input() -> dict:
    """Fully current, complete, synthetic rule input (approval-eligible)."""
    return {
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
    }


def statuses(result: dict) -> list[str]:
    return [check["status"] for check in result["checks"]]


class EvaluatorShapeTests(unittest.TestCase):
    def test_shape_names_and_order_preserved(self) -> None:
        result = evaluate(current_complete_input())
        self.assertEqual(len(result["checks"]), 6)
        self.assertEqual([c["name"] for c in result["checks"]], EXPECTED_NAMES)
        decision = result["decision"]
        for key in ("verdict", "token_class", "fiat_class", "token_text", "fiat_text", "repair_text"):
            self.assertIn(key, decision)


class EvaluatorCaseTests(unittest.TestCase):
    def test_case_blocked(self) -> None:
        data = current_complete_input()
        data["authority_status"] = "expiring_soon"
        data["allowlist_status"] = "stale"
        data["payload_status"] = "incomplete"
        result = evaluate(data)
        self.assertEqual(result["decision"]["verdict"], BLOCKED)
        self.assertEqual(statuses(result), ["good", "good", "warn", "bad", "bad", "good"])

    def test_case_refreshed(self) -> None:
        result = evaluate(current_complete_input())
        self.assertEqual(result["decision"]["verdict"], APPROVED)
        self.assertEqual(statuses(result), ["good"] * 6)

    def test_case_authority_expired(self) -> None:
        data = current_complete_input()
        data["authority_status"] = "expired"
        result = evaluate(data)
        self.assertEqual(result["decision"]["verdict"], AUTHORITY_HOLD)
        # Payload is complete but the expired authority chain downgrades it to warn.
        self.assertEqual(statuses(result), ["good", "good", "bad", "good", "warn", "good"])

    def test_case_missing_fallback(self) -> None:
        data = current_complete_input()
        del data["fallback_intermediary_bic"]
        result = evaluate(data)
        self.assertEqual(result["decision"]["verdict"], INSUFFICIENT_HOLD)
        self.assertNotEqual(result["decision"]["verdict"], APPROVED)
        self.assertEqual(result["checks"][5]["status"], "bad")

    def test_case_expired_authority_plus_stale_allowlist(self) -> None:
        data = current_complete_input()
        data["authority_status"] = "expired"
        data["allowlist_status"] = "stale"
        decision = evaluate(data)["decision"]
        # Hard token-route block takes precedence over the authority-expired hold.
        self.assertEqual(decision["verdict"], BLOCKED)
        # The explanation must name both simultaneous problems, not just the allowlist.
        token = decision["token_text"].lower()
        self.assertIn("stale", token)
        self.assertIn("allowlist", token)
        self.assertIn("expired", token)
        self.assertIn("authority", token)
        # Repair must require both allowlist refresh and authority renewal/refresh.
        repair = decision["repair_text"].lower()
        self.assertIn("allowlist", repair)
        self.assertIn("refresh", repair)
        self.assertIn("authority", repair)
        self.assertIn("renew", repair)


class EvaluatorSafetyTests(unittest.TestCase):
    def test_unknown_status_holds_and_never_approves(self) -> None:
        data = current_complete_input()
        data["authority_status"] = "frozen_pending_review"
        result = evaluate(data)
        self.assertEqual(result["decision"]["verdict"], INSUFFICIENT_HOLD)
        self.assertNotEqual(result["decision"]["verdict"], APPROVED)
        self.assertEqual(result["checks"][2]["status"], "bad")

    def test_missing_required_input_holds(self) -> None:
        data = current_complete_input()
        data["legal_entity_present"] = False
        result = evaluate(data)
        self.assertEqual(result["decision"]["verdict"], INSUFFICIENT_HOLD)

    def test_expiring_soon_without_blocker_never_approves(self) -> None:
        data = current_complete_input()
        data["authority_status"] = "expiring_soon"
        result = evaluate(data)
        self.assertNotEqual(result["decision"]["verdict"], APPROVED)


class EvaluatorMutationTests(unittest.TestCase):
    def test_field_changes_change_the_result(self) -> None:
        baseline = evaluate(current_complete_input())["decision"]["verdict"]
        self.assertEqual(baseline, APPROVED)

        stale = current_complete_input()
        stale["allowlist_status"] = "stale"
        stale_verdict = evaluate(stale)["decision"]["verdict"]
        self.assertEqual(stale_verdict, BLOCKED)
        self.assertNotEqual(stale_verdict, baseline)

        incomplete = current_complete_input()
        incomplete["payload_status"] = "incomplete"
        self.assertEqual(evaluate(incomplete)["decision"]["verdict"], BLOCKED)

        expired = current_complete_input()
        expired["authority_status"] = "expired"
        expired_verdict = evaluate(expired)["decision"]["verdict"]
        self.assertEqual(expired_verdict, AUTHORITY_HOLD)
        self.assertNotEqual(expired_verdict, baseline)

        dropped = current_complete_input()
        dropped["fallback_currency"] = ""
        self.assertEqual(evaluate(dropped)["decision"]["verdict"], INSUFFICIENT_HOLD)


class EvaluatorInputValidationTests(unittest.TestCase):
    def test_unsupported_presence_types_hold(self) -> None:
        # Only the Python bool True counts as present; strings/ints must not approve.
        for bad in ("false", "true", 1, 0, None):
            data = current_complete_input()
            data["institution_present"] = bad
            result = evaluate(data)
            self.assertEqual(result["decision"]["verdict"], INSUFFICIENT_HOLD, bad)
            self.assertNotEqual(result["decision"]["verdict"], APPROVED)

    def test_unsupported_legal_entity_flag_holds(self) -> None:
        data = current_complete_input()
        data["legal_entity_present"] = "true"
        self.assertEqual(evaluate(data)["decision"]["verdict"], INSUFFICIENT_HOLD)

    def test_non_string_fallback_values_hold(self) -> None:
        for field, bad in (("fallback_currency", 123), ("fallback_intermediary_bic", True), ("fallback_rail", ["x"])):
            data = current_complete_input()
            data[field] = bad
            result = evaluate(data)
            self.assertEqual(result["decision"]["verdict"], INSUFFICIENT_HOLD, (field, bad))
            self.assertEqual(result["checks"][5]["status"], "bad")

    def test_none_input_holds_without_exception(self) -> None:
        result = evaluate(None)
        self.assertEqual(result["decision"]["verdict"], INSUFFICIENT_HOLD)
        self.assertEqual(len(result["checks"]), 6)

    def test_empty_input_holds(self) -> None:
        result = evaluate({})
        self.assertEqual(result["decision"]["verdict"], INSUFFICIENT_HOLD)
        self.assertNotEqual(result["decision"]["verdict"], APPROVED)

    def test_unhashable_or_nonstring_status_values_hold(self) -> None:
        # Unhashable values (list/dict) must not raise on set membership, and no
        # non-string status value may approve.
        for field in ("authority_status", "allowlist_status", "payload_status"):
            for bad in ([], {}, ["current"], {"status": "current"}, 7):
                data = current_complete_input()
                data[field] = bad
                result = evaluate(data)  # must not raise
                self.assertEqual(result["decision"]["verdict"], INSUFFICIENT_HOLD, (field, bad))
                self.assertNotEqual(result["decision"]["verdict"], APPROVED)


class EvaluatorClaimAccuracyTests(unittest.TestCase):
    def test_missing_fallback_does_not_select_or_approve_fiat(self) -> None:
        data = current_complete_input()
        del data["fallback_intermediary_bic"]
        decision = evaluate(data)["decision"]
        self.assertEqual(decision["verdict"], INSUFFICIENT_HOLD)
        self.assertNotEqual(decision["fiat_class"], "selected")
        self.assertNotIn("selected", decision["fiat_text"].lower())
        self.assertNotIn("approved", decision["fiat_text"].lower())
        self.assertNotIn("approved", decision["token_text"].lower())

    def test_stale_only_blocker_text_omits_payload_claim(self) -> None:
        data = current_complete_input()
        data["allowlist_status"] = "stale"
        decision = evaluate(data)["decision"]
        self.assertEqual(decision["verdict"], BLOCKED)
        self.assertNotIn("incomplete", decision["token_text"].lower())
        self.assertNotIn("incomplete", decision["repair_text"].lower())

    def test_payload_only_blocker_text_omits_allowlist_claim(self) -> None:
        data = current_complete_input()
        data["payload_status"] = "incomplete"
        decision = evaluate(data)["decision"]
        self.assertEqual(decision["verdict"], BLOCKED)
        self.assertNotIn("stale", decision["token_text"].lower())
        self.assertNotIn("allowlist", decision["token_text"].lower())

    def test_both_blockers_name_each_required_repair(self) -> None:
        data = current_complete_input()
        data["allowlist_status"] = "stale"
        data["payload_status"] = "incomplete"
        decision = evaluate(data)["decision"]
        self.assertEqual(
            decision["token_text"],
            "Blocked. Wallet allowlist is stale and endpoint-control evidence is incomplete.",
        )
        self.assertEqual(
            decision["fiat_text"],
            "Selected now. The fiat route remains approved and traceable while the digital endpoint is repaired.",
        )
        self.assertEqual(
            decision["repair_text"],
            "Repair task: refresh the wallet allowlist and complete the endpoint-control payload before using the tokenized route.",
        )

    def test_expiring_soon_is_insufficient_with_non_expired_wording(self) -> None:
        data = current_complete_input()
        data["authority_status"] = "expiring_soon"
        decision = evaluate(data)["decision"]
        self.assertEqual(decision["verdict"], INSUFFICIENT_HOLD)
        self.assertNotEqual(decision["verdict"], APPROVED)
        self.assertNotIn("expired", decision["token_text"].lower())
        self.assertNotIn("selected", decision["fiat_text"].lower())


class EvaluatorMalformedFallbackTests(unittest.TestCase):
    """SEC-P10 regression: malformed-but-nonempty fallback SSI fields must not
    silently approve. Each fallback field is mutated, in isolation, to an
    unambiguously malformed synthetic value that is still a nonblank string,
    preventing regression to a presence-only check. A malformed fallback SSI
    field means the fiat fallback is not actually traceable, so the Fiat SSI
    fallback check must be "bad" and the route must HOLD, never approve.
    """

    # Each value is nonblank (survives .strip()) yet impossible for its field.
    MALFORMED = {
        # ISO 4217 alpha-3 requires exactly three letters; four is malformed.
        "fallback_currency": "EURO",
        # A BIC is 8 or 11 alphanumerics starting with a 4-letter bank code.
        "fallback_intermediary_bic": "123",
        # An IBAN-style mask must carry a country code and masked digits.
        "fallback_account_mask": "??",
        # A rail label cannot be punctuation only.
        "fallback_rail": "###",
    }

    def _assert_malformed_holds(self, field: str, bad: str) -> None:
        data = current_complete_input()
        data[field] = bad
        result = evaluate(data)
        decision = result["decision"]
        self.assertEqual(decision["verdict"], INSUFFICIENT_HOLD, (field, bad))
        # Guard the exact silent-approval regression Hari flagged.
        self.assertNotEqual(decision["verdict"], APPROVED, (field, bad))
        self.assertEqual(result["checks"][5]["name"], "Fiat SSI fallback")
        self.assertEqual(result["checks"][5]["status"], "bad", (field, bad))

    def test_malformed_fallback_rail_holds(self) -> None:
        self._assert_malformed_holds("fallback_rail", self.MALFORMED["fallback_rail"])

    def test_malformed_fallback_currency_holds(self) -> None:
        self._assert_malformed_holds("fallback_currency", self.MALFORMED["fallback_currency"])

    def test_malformed_fallback_account_mask_holds(self) -> None:
        self._assert_malformed_holds("fallback_account_mask", self.MALFORMED["fallback_account_mask"])

    def test_malformed_fallback_intermediary_bic_holds(self) -> None:
        self._assert_malformed_holds("fallback_intermediary_bic", self.MALFORMED["fallback_intermediary_bic"])


if __name__ == "__main__":
    unittest.main()
