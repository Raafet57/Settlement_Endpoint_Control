#!/usr/bin/env python3
"""SEC-P10 deterministic route evaluator.

Pure function: it derives the six policy checks and the route decision from
synthetic endpoint / authority / allowlist / payload / fallback field values
only. It never inspects a scenario slug, and it never approves on unknown,
unsupported, or insufficient input.
"""
from __future__ import annotations

# Public verdicts. The first three are the existing shipped verdicts and must
# stay byte-for-byte. INSUFFICIENT_INPUT_MANUAL_HOLD covers unknown/missing input.
APPROVED = "TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED"
BLOCKED = "TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED"
AUTHORITY_HOLD = "AUTHORITY_EXPIRED_MANUAL_HOLD"
INSUFFICIENT_HOLD = "INSUFFICIENT_INPUT_MANUAL_HOLD"

# Check names, in display order. These equal the route_policies names and must
# be preserved.
CHECK_NAMES = (
    "BIC and institution shape",
    "LEI and counterparty profile",
    "vLEI-style authority evidence",
    "Wallet allowlist freshness",
    "Endpoint-control payload",
    "Fiat SSI fallback",
)

AUTHORITY_KNOWN = {"current", "expiring_soon", "expired"}
ALLOWLIST_KNOWN = {"current", "stale"}
PAYLOAD_KNOWN = {"complete", "incomplete"}

REQUIRED_FALLBACK_FIELDS = (
    "fallback_rail",
    "fallback_currency",
    "fallback_account_mask",
    "fallback_intermediary_bic",
)

# Explicit synthetic fallback contract: each required field supports exactly its
# one canonical synthetic value. Any other value (malformed, unknown, empty, or a
# non-string type) is unsupported and must fail closed. This is deliberate exact
# membership for the demo fixtures, not broad real-provider format validation.
SUPPORTED_FALLBACK_VALUES = {
    "fallback_rail": "Fiat SSI route",
    "fallback_currency": "EUR",
    "fallback_account_mask": "DE•• •••• •••• 4400",
    "fallback_intermediary_bic": "INTERDEFFXXX",
}


def _is_true(value: object) -> bool:
    # Only the Python bool True is a supported presence flag; strings such as
    # "false"/"true" and ints such as 0/1 are unsupported and must not approve.
    return value is True


def _known(value: object, allowed: set) -> bool:
    # Require a string before set membership so unhashable values (list/dict) and
    # other unsupported types hold rather than raising TypeError.
    return isinstance(value, str) and value in allowed


def _fallback_complete(rule_input: dict) -> bool:
    # Exact-equality match against the single supported synthetic value per field.
    # Equality (not set membership) so unhashable values (list/dict) compare False
    # rather than raising, and any malformed/unsupported value fails closed.
    return all(rule_input.get(field) == SUPPORTED_FALLBACK_VALUES[field] for field in REQUIRED_FALLBACK_FIELDS)


def _check(status: str, name: str, detail: str) -> dict:
    return {"status": status, "name": name, "detail": detail}


def _decision(verdict: str, token_class: str, fiat_class: str, token_text: str, fiat_text: str, repair_text: str) -> dict:
    return {
        "verdict": verdict,
        "token_class": token_class,
        "fiat_class": fiat_class,
        "token_text": token_text,
        "fiat_text": fiat_text,
        "repair_text": repair_text,
    }


def _approved_decision() -> dict:
    return _decision(
        APPROVED,
        "selected",
        "",
        "Approved. Wallet allowlist, authority evidence and endpoint-control payload are current.",
        "Retained as fallback. No immediate route switch required.",
        "Repair task closed: endpoint evidence is current. Continue monitoring freshness before future releases.",
    )


def _authority_hold_decision() -> dict:
    return _decision(
        AUTHORITY_HOLD,
        "warned",
        "selected",
        "Hold. Endpoint allowlist is current, but authority evidence is expired and requires risk review.",
        "Selected if settlement must proceed before authority repair.",
        "Repair task: renew authority evidence or route through fallback with manual approval.",
    )


def _blocked_decision(allowlist_stale: bool, payload_incomplete: bool, authority_expired: bool) -> dict:
    # Derive the blocker sentence from the actual hard-block fields. At least one
    # hard blocker is always set here (the caller guarantees it).
    if not authority_expired:
        # Authority not expired: preserve the exact shipped blocked-scenario text.
        if allowlist_stale and payload_incomplete:
            token_text = "Blocked. Wallet allowlist is stale and endpoint-control evidence is incomplete."
            repair_text = "Repair task: refresh wallet allowlist and endpoint authority evidence before using the tokenized route."
        elif allowlist_stale:
            token_text = "Blocked. Wallet allowlist is stale for the requested counterparty and rail."
            repair_text = "Repair task: refresh the wallet allowlist before using the tokenized route."
        else:
            token_text = "Blocked. Endpoint-control evidence is incomplete."
            repair_text = "Repair task: complete the endpoint-control payload before using the tokenized route."
    else:
        # Authority is also expired: name every simultaneous problem and require
        # every corresponding repair action.
        if allowlist_stale and payload_incomplete:
            token_text = "Blocked. Wallet allowlist is stale, endpoint-control evidence is incomplete, and authority evidence is expired."
            repair_text = "Repair task: refresh the wallet allowlist, complete the endpoint-control payload, and renew authority evidence before using the tokenized route."
        elif allowlist_stale:
            token_text = "Blocked. Wallet allowlist is stale and authority evidence is expired."
            repair_text = "Repair task: refresh the wallet allowlist and renew authority evidence before using the tokenized route."
        else:
            token_text = "Blocked. Endpoint-control evidence is incomplete and authority evidence is expired."
            repair_text = "Repair task: complete the endpoint-control payload and renew authority evidence before using the tokenized route."
    # Fallback is known complete for any BLOCKED outcome, so fiat selection is accurate.
    return _decision(
        BLOCKED,
        "blocked",
        "selected",
        token_text,
        "Selected now. The fiat route remains approved and traceable while the digital endpoint is repaired.",
        repair_text,
    )


def _insufficient_decision(reason: str) -> dict:
    # Both routes are held (warned); neither token nor fiat is selected/approved,
    # so no false fallback-selection claim is made.
    if reason == "freshness":
        token_text = "Hold. Authority evidence expires soon and must be refreshed before token use."
        fiat_text = "Held for manual review; the fiat fallback is not engaged while authority freshness is confirmed."
        repair_text = "Repair task: refresh authority evidence before using the tokenized route."
    else:
        token_text = "Hold. Required endpoint input is missing or unsupported; automated release is not permitted."
        fiat_text = "Held for manual review; the fiat fallback is not engaged while endpoint inputs remain incomplete."
        repair_text = "Repair task: supply complete, supported endpoint, authority, allowlist and fallback inputs before token use."
    return _decision(INSUFFICIENT_HOLD, "warned", "warned", token_text, fiat_text, repair_text)


def _institution_check(rule_input: dict) -> dict:
    ok = _is_true(rule_input.get("institution_present")) and _is_true(rule_input.get("institution_reachable"))
    if ok:
        return _check("good", CHECK_NAMES[0], "Institution layer is present and reaches fallback SSI holder.")
    return _check("bad", CHECK_NAMES[0], "Institution shape or reachability evidence is missing.")


def _legal_entity_check(rule_input: dict) -> dict:
    if _is_true(rule_input.get("legal_entity_present")):
        return _check("good", CHECK_NAMES[1], "Legal entity profile is present for the beneficiary.")
    return _check("bad", CHECK_NAMES[1], "Legal entity profile is missing for the beneficiary.")


def _authority_check(authority: str) -> dict:
    if authority == "current":
        return _check("good", CHECK_NAMES[2], "Synthetic authority evidence is current.")
    if authority == "expiring_soon":
        return _check("warn", CHECK_NAMES[2], "Authority evidence expires soon and needs refresh before token use.")
    if authority == "expired":
        return _check("bad", CHECK_NAMES[2], "Authority evidence is expired in this synthetic scenario.")
    return _check("bad", CHECK_NAMES[2], "Authority evidence status is unknown or unsupported.")


def _allowlist_check(allowlist: str) -> dict:
    if allowlist == "current":
        return _check("good", CHECK_NAMES[3], "Allowlist is current for this counterparty and rail.")
    if allowlist == "stale":
        return _check("bad", CHECK_NAMES[3], "Allowlist is stale for the requested counterparty and rail.")
    return _check("bad", CHECK_NAMES[3], "Allowlist status is unknown or unsupported.")


def _payload_check(payload: str, authority: str) -> dict:
    if payload == "incomplete":
        return _check("bad", CHECK_NAMES[4], "Beneficiary wallet-control field is incomplete.")
    if payload == "complete":
        if authority == "expired":
            return _check("warn", CHECK_NAMES[4], "Payload complete, but authority chain blocks release.")
        return _check("good", CHECK_NAMES[4], "Required synthetic payload is complete.")
    return _check("bad", CHECK_NAMES[4], "Endpoint-control payload status is unknown or unsupported.")


def _fallback_check(rule_input: dict) -> dict:
    if _fallback_complete(rule_input):
        return _check("good", CHECK_NAMES[5], "Fallback route is available and traceable.")
    return _check("bad", CHECK_NAMES[5], "Fallback route is incomplete; a required SSI field is missing or unsupported.")


def _route(rule_input: dict) -> dict:
    authority = rule_input.get("authority_status")
    allowlist = rule_input.get("allowlist_status")
    payload = rule_input.get("payload_status")

    supported = (
        _is_true(rule_input.get("institution_present"))
        and _is_true(rule_input.get("institution_reachable"))
        and _is_true(rule_input.get("legal_entity_present"))
        and _known(authority, AUTHORITY_KNOWN)
        and _known(allowlist, ALLOWLIST_KNOWN)
        and _known(payload, PAYLOAD_KNOWN)
        and _fallback_complete(rule_input)
    )
    if not supported:
        return _insufficient_decision("input")

    # Hard token-route block (stale allowlist or incomplete payload). Fallback is
    # already known complete here, so this precedes the authority-expired hold.
    if allowlist == "stale" or payload == "incomplete":
        return _blocked_decision(allowlist == "stale", payload == "incomplete", authority == "expired")

    # Actual expired authority is a named risk-review hold.
    if authority == "expired":
        return _authority_hold_decision()

    # Any other non-current authority (e.g. expiring soon) is insufficient
    # freshness for automated release, not an expiry; hold without expired wording.
    if authority != "current":
        return _insufficient_decision("freshness")

    return _approved_decision()


def evaluate(rule_input: dict) -> dict:
    """Compute the ordered policy checks and route decision from field inputs.

    A non-dict top-level input (e.g. None) is treated as unknown/insufficient and
    yields a deterministic HOLD response rather than raising.
    """
    if not isinstance(rule_input, dict):
        rule_input = {}
    authority = rule_input.get("authority_status")
    checks = [
        _institution_check(rule_input),
        _legal_entity_check(rule_input),
        _authority_check(authority),
        _allowlist_check(rule_input.get("allowlist_status")),
        _payload_check(rule_input.get("payload_status"), authority),
        _fallback_check(rule_input),
    ]
    return {"checks": checks, "decision": _route(rule_input)}
