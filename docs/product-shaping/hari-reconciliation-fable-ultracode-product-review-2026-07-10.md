# Hari reconciliation — Fable UltraCode product review

**Date:** 2026-07-10  
**Frozen product snapshot:** `94986ed3e6670b2db0f9e29bde3b994a68b60b9a`  
**Primary artifacts:**

- `fable-ultracode-product-review-2026-07-10.md`
- `fable-ultracode-product-review-evidence-2026-07-10.json`

## Bottom line

**Conditional PRODUCTIZE. Enter as an advisory control, not a live blocking gate.**

The strongest product is an internal pre-release endpoint control gate: compute whether a tokenized settlement endpoint is eligible, held, or blocked; preserve a valid fiat fallback; explain the result; and emit an audit-ready receipt.

The current repository proves the workflow and narrative, but it does not yet prove the product. It switches among authored scenarios; it does not derive a decision from endpoint state. The first product milestone is therefore not hosting, branding, or another integration. It is a deterministic rule evaluator.

## Model/run reconciliation

The run was a real interactive UltraCode workflow on Claude Code `2.1.206`:

- Started with Fable 5 as lead.
- `/effort ultracode` was visibly enabled with dynamic workflows.
- Auto mode was visibly on.
- Seven independent Sonnet 5 scouts completed.
- Two independent Sonnet 5 adversarial agents completed.
- True fanout was observed; this was not a sequential simulation.

One correction is required to the Fable-generated artifact metadata: after five scouts had reported, a Fable safeguard event switched the lead session to Opus 4.8. The session JSONL records one `model_refusal_fallback` event, 70 Fable assistant messages, 45 Opus 4.8 assistant messages, and Opus 4.8 as the final synthesis model.

Therefore the accurate label is:

> **Fable-led UltraCode review with seven Sonnet 5 scouts, two Sonnet 5 adversarial agents, and an Opus 4.8 fallback for final synthesis/validation.**

The main report and evidence JSON remain the immutable worker artifacts; this note is the canonical metadata correction.

## Hari cold-review verdict

Repository evidence supports the central findings:

1. **No decision engine.** Checks and route verdicts are hardcoded scenario fixtures in `demo-db/seed.py:172-233` and `demo/index.html:475-539`; `load_scenario()` reads stored rows without evaluation.
2. **No endpoint lifecycle.** All scenarios share one endpoint row, there is no endpoint CRUD, and status fields are scenario placeholders.
3. **No enforced authority.** Role selection is presentation-only; the API accepts a client-asserted actor and performs no authentication or maker-checker validation.
4. **No decision history.** `route_decisions.scenario_id` is unique, so the schema cannot represent re-validation or a second decision.
5. **Operator actions do not change the verdict.** They append action/audit rows only.
6. **A real localhost security chain exists.** Hari reproduced a cross-origin-style `text/plain` JSON write with an arbitrary action value, confirmed the raw value was persisted, and confirmed the DB UI places that value into an `innerHTML` sink. Localhost binding limits exposure today; this must be fixed before any broader exposure.
7. **Product-grade controls are absent.** No tenancy, migration mechanism, tamper-evident ledger, CI, structured logging, or public-backend security posture exists.

Hari independently reran:

```text
STATIC_QA PASS
DB SMOKE PASS
cold verifier PASS
17 report findings parsed
retired framing hits: 0
remote main unchanged
open pull requests: 0
generated DB/cache artifacts: cleaned
```

## Product shape

### Destination

**Shape A — internal pre-release endpoint control gate.**

This preserves the differentiated control moment: do not merely flag a risky endpoint; compute a route decision, preserve a working fallback, and create evidence.

### Entry posture

**Shape C behavior — advisory-only observation and policy simulation.**

The product should initially calculate and record what it *would* block or hold without controlling real value movement. This is the lower-liability path for learning false-positive rates, workflow ownership, and buyer demand.

### Do not build yet

- Shared cross-institution registry.
- Public backend.
- Payment execution.
- Live identity, authority, wallet, screening, or chain-analytics claims.
- Multi-tenant enterprise platform shell before the core evaluator works.

## Correctly split next gates

The Fable artifact combines local implementation and buyer outreach in one acceptance gate. Hari separates them because they have different approval and side-effect boundaries.

### Recommended next local gate

```text
GO SETTLEMENT GATE-1 LOCAL EVALUATOR
```

Scope:

- `demo-db/` only.
- Deterministic evaluator derives checks and all three existing verdicts from input fields.
- Add two synthetic edge cases: missing fallback; expired authority plus stale allowlist.
- Extended smoke proves 5/5 expected computed outcomes.
- Keep localhost, synthetic data, no real providers, no auth expansion, no deploy, no commit, and no push unless separately approved.

### Separate market-validation gate

```text
GO SETTLEMENT DESIGN-PARTNER DISCOVERY
```

That later gate would prepare and, only if explicitly approved, use a short discovery script with at least three payment/treasury-operations contacts. The decisive question is whether institutions already cover most of the six controls and whether the token-versus-fiat routing fork occurs in a workflow they operate today.

## Remaining Raf decisions

1. Is the intended destination advisory-only, or eventual enforced blocking after evidence and liability ownership exist?
2. Which first design-partner profile matters most: originating bank, digital-asset custodian, or creditor/fallback holder?
3. Which genuinely new check should become real first: wallet-allowlist freshness or authority evidence?

## Gate state

```text
Local product review: complete
Local report artifacts: ready for Raf review
Commit: not approved
Push: not approved
PR: not approved
Deploy/public backend: blocked
Live integrations/data: blocked
Market outreach: not approved
```
