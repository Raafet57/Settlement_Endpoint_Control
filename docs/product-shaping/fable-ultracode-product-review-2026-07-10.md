# Settlement Endpoint Control Tower — UltraCode product-shaping review

**Review date:** 2026-07-10
**Frozen snapshot:** `94986ed3e6670b2db0f9e29bde3b994a68b60b9a` (branch `main`, 22 tracked files)
**Nature:** product-strategy and technical-productization review of a synthetic reference demo — not an implementation sprint.

## Workflow and model topology

This review used a lead-plus-fanout ("UltraCode" dynamic) workflow.

- **Lead / synthesizer:** `claude-fable-5`, effort `ultracode`.
- **Phase 1 — seven independent scouts, each `claude-sonnet-5`:** product-thesis, demo-ux, architecture, safety-claims, business-gtm, productization-roadmap, quality-deploy. Status: all 7 completed.
- **Phase 2 — two independent adversarial agents, each `claude-sonnet-5`:** skeptic-verifier (reproduce/refute) and product-redteam (kill the thesis). Status: both completed.
- **Totals:** 9 review agents across 2 fanout phases under 1 lead; each agent ran read-only against the frozen tree and self-reported its model id.

Every serious scout finding was independently re-checked by the skeptic-verifier against the cited `file:line` before it entered the findings table below. Where the roadmap scout and the red-team disagreed on the wedge, both positions are carried into the verdict and the decision box rather than averaged away.

Read-only checks executed by the lead and verifier: `python3 demo/qa_static_demo.py` → `STATIC_QA PASS`; `python3 demo-db/smoke.py` → `SMOKE PASS`; case-insensitive whole-tree scan for the six retired terms → zero occurrences of each.

---

## 1. Executive verdict

**PRODUCTIZE — conditional and advisory-first. Confidence: MODERATE (leaning low).**

The underlying idea earns continued investment, but the repository today is a *pre-product reference demo*, not a product: it renders three hand-authored outcomes, it computes nothing, and every component a buyer would pay for is still to be built. The path forward is real but narrow, and it must start advisory-only.

- **Biggest upside:** the control moment is specific and non-generic — a valid beneficiary and a valid fiat standing settlement instruction, but a tokenized endpoint that is unsafe or incomplete, so the token route is blocked or held, the reason is explained, the fiat fallback is preserved, and an evidence receipt is emitted. That decision is modeled coherently end-to-end across doc, UI, schema, and evidence export, wrapped in unusually disciplined, test-enforced claim hygiene.
- **Biggest risk:** there is no decision logic. Checks and verdicts are literal fixtures keyed by scenario slug (`demo-db/seed.py:172-233`, duplicated in `demo/index.html:475-539`); nothing derives a verdict from an endpoint's field values. Around that gap sit no endpoint CRUD, cosmetic four-eyes, zero authentication, no tenancy, no tamper-evident audit, and no live data. The demonstrated shape is ~1,150 lines and easy for a bank platform team to reproduce, so defensibility must come from verified data and enforced workflow, never from the demo.
- **Honest dissent on record:** as a *live blocking gate deployed as-is*, the red-team's verdict is STOP, and it is correct — an enforcement point built on zero-auth, cosmetic dual-control, and non-tamper-evident audit is not pilotable. This review honors that by recommending advisory-mode-first and by making the rule engine the single next gate. If the cheap demand tests in §12 fail, the correct action flips to PIVOT (observe-only, Shape C) or STOP.
- **One-sentence next move:** build a deterministic rule evaluator that computes the three existing verdicts from endpoint field values — replacing the hardcoded fixtures — while everything else stays synthetic and localhost.

---

## 2. Current-state fact map

**What exists (verified):**
- A self-contained static browser demo (`demo/index.html`, 690 lines): four views (endpoint profile, pre-validation, route decision, evidence/audit), three scenarios (blocked, refreshed, authority-expired), three role labels (ops analyst, risk reviewer, four-eyes approver), a guided walkthrough, and client-side evidence/receipt export. No network calls; CSP `connect-src 'none'` (`demo/index.html:6`).
- A localhost-only SQLite workflow (`demo-db/`): schema v3 across 11 tables (`seed.py:36-161`), seven read/append API routes (`app.py:213-268`), persisted operator actions, an audit append that grows on each action (`app.py:139-168`), and a DB-backed evidence export (`app.py:171-186`). Enforced localhost bind via `SystemExit` (`app.py:276-277`).
- Test-enforced claim discipline: `demo/qa_static_demo.py:47-58` fails the check if overclaim terms appear; `smoke.py` asserts seed idempotency and exact row counts.

**What is represented (synthetic):** BIC-shaped institution identifiers, LEI/vLEI-style authority summaries, wallet/custody/allowlist states, beneficiary-data/Travel-Rule completeness, fiat SSI fallback instructions, and ISO 20022/UETR-style trace events. Per-row `source_lineage` disclosures mark each as synthetic (`seed.py:17-34`). No external source rows (`source_manifest.json` — `data_row_estimate: 0` for all four concept sources).

**What is absent:** a policy/decision engine; endpoint-profile CRUD (a single shared `endpoint_id=1` underlies all three scenarios); enforced four-eyes; any authentication or authorization; tenancy; a tamper-evident evidence ledger; schema migrations; CI; live integrations; a data-retention policy; and per-instance receipt identifiers.

**Strongest point:** the "blocked" scenario is where copy, visual route state, and exportable evidence all tell the same story — fiat valid, token endpoint unsafe, fallback preserved (`demo/index.html:402,433-434,487`; `seed.py:183`) — reinforced by claim discipline that a compliance reviewer would respect.

**Weakest point:** nothing is computed. Switching the scenario dropdown swaps to a different pre-written blob; clicking an operator action writes an audit row but leaves the verdict and checks byte-for-byte unchanged (`app.py:139-168` contains no `UPDATE`/`DELETE`).

---

## 3. Product category and wedge

- **Category:** a *pre-release tokenized-settlement endpoint control gate* — not a dashboard, not a generic compliance wrapper.
- **First controlled workflow:** for a single outbound instruction, evaluate the identity-bound endpoint profile before token release; block or hold if control evidence is stale or incomplete; automatically preserve or select the fiat fallback; emit an audit receipt.
- **Positioning sentence:** "A pre-release control point that blocks or holds a tokenized settlement instruction when wallet or authority evidence is stale, while keeping the already-approved fiat fallback live — with an audit-ready decision receipt."
- **Why this wedge beats a generic dashboard or compliance wrapper:** the artifact is a *decision plus a preserved working path* (`TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED`, `..._APPROVED_FIAT_FALLBACK_RETAINED`, `AUTHORITY_EXPIRED_MANUAL_HOLD`), not a status board or a flag with no next action.
- **Red-team caveat that must shape positioning:** the six-check taxonomy (`seed.py:163-170`) overlaps heavily with controls banks already run for fiat (SSI validation, LEI/counterparty screening, authority/dual-control, fallback SSI). Only two axes are genuinely new — **wallet allowlist freshness** and **endpoint-control payload completeness**. The wedge must lead with those two, or it risks being read as a rebrand of existing standing-settlement-instruction change control.

---

## 4. Actors and jobs

Grounded in the roles the repo actually models:

| Actor | In-repo evidence | Job in the first product |
|---|---|---|
| Economic buyer (HYPOTHESIS — not modeled) | inferred from treasury-ops framing (`seed.py:174,194`) | fund a control that prevents misdirected tokenized settlement and reduces audit effort |
| Daily operator (ops analyst) | `demo/index.html:362,471`; default actor `ops_analyst` (`app.py:141`) | run pre-release validation; open repair; select fallback |
| Risk / compliance reviewer | `demo/index.html:363,472`; "Risk reviewer queue" (`seed.py:214`) | review holds; decide renew-vs-override; own the audit narrative |
| Approver (four-eyes) | `demo/index.html:364,473` — currently cosmetic | independently authorize a release/override after the exception clears |
| Implementation owner (HYPOTHESIS) | expansion gates (`docs/product-spine.md:55`) | own integration and accept the gated-future seams |
| Counterparties | debtor/creditor agents, beneficiary entity (`seed.py:269-276`) | affected parties whose settlement is blocked, held, or fallen back |

**Job-to-be-done (best-evidenced):** "Before releasing value on a tokenized rail, tell me in one place whether this beneficiary's wallet endpoint has current, sufficient control evidence — and if not, don't dead-end the payment: route it to the already-approved fiat fallback and hand me a defensible reason plus an audit record." The "endpoint profile" object maps in real operations to a **standing settlement instruction record extended with wallet/custody and authority-evidence layers**.

---

## 5. Value and ROI hypotheses

All are labeled hypotheses; the repo contains no telemetry, prices, or customer data, so none is asserted as a number. Each names the pilot metric that would validate it.

- **H1 — Misdirected tokenized-settlement prevention.** Metric: count/value of token instructions that would have released to an endpoint with stale/incomplete evidence, caught pre-release. Validation: shadow-mode run of the six checks against real instructions for a pilot window vs. the partner's exception/loss log. Evidence: `seed.py:179-183`.
- **H2 — Fiat-fallback continuity.** Metric: percentage of blocked token routes that still settle same value-date via the preserved fallback; time-to-fallback decision. Validation: before/after timing. Evidence: `seed.py:69-72,183`.
- **H3 — Triage-time reduction.** Metric: minutes from "endpoint flagged" to "decision or repair opened." Validation: time-motion vs. current ad hoc process. Evidence: named checks + role guidance (`seed.py:163-170`; `demo/index.html:470-474`).
- **H4 — Audit-preparation effort reduction.** Metric: hours to assemble an audit packet for one exception. Validation: before/after on a sampled real exception. Evidence: `evidence_export()` (`app.py:171-186`).
- **H5 — Segregation-of-duties (weak; gated on enforcement).** Metric: override-without-resolution rate. Validation: only meaningful once four-eyes is server-enforced; today unimplemented (`app.py:139-168`).
- **H6 — Beneficiary-data completeness reducing rework.** Metric: beneficiary-data return/rework rate. Validation: before/after. Evidence: `endpoint_payload_status` + Travel-Rule lineage (`seed.py:67`; `source_manifest.json:37-42`).

---

## 6. Three distinct shapes and the R×S fit matrix

**Requirements for a first sellable product:**

- **R0** govern endpoint profiles as first-class, multi-instance records
- **R1** pre-release validation checks *computed* from field state
- **R2** block/hold/approve decision states with explanation
- **R3** fallback preservation/selection
- **R4** append-only, tamper-evident evidence/audit
- **R5** enforced four-eyes / authority separation
- **R6** policy configurability per institution
- **R7** integration seams to real data sources
- **R8** deployable, secure, tenant-isolated service

**Shapes:**
- **A — internal pre-release endpoint control gate** inside one institution's payment-ops flow (the demonstrated shape).
- **B — shared cross-counterparty endpoint-profile registry/directory** (institutions publish/verify endpoint profiles others query).
- **C — observe-only audit/evidence and policy-simulation layer** that scores and records but never gates.

**Binary R × S fit** (Y = requirement is central to the shape's control loop and the shape can express it; N = outside the shape's control surface):

| Req | A | B | C |
|---|---|---|---|
| R0 first-class profiles | Y | Y | N |
| R1 computed checks | Y | N | Y |
| R2 block/hold/approve | Y | N | N |
| R3 fallback selection | Y | N | N |
| R4 tamper-evident audit | Y | Y | Y |
| R5 enforced four-eyes | Y | N | N |
| R6 per-institution policy | Y | Y | Y |
| R7 real data seams | Y | Y | Y |
| R8 secure/tenant-isolated | Y | Y | Y |
| **Fit** | **9/9** | **5/9** | **5/9** |

**Shape risks:** A — no evaluator exists yet; false block/approve carries operational risk; four-eyes and auth must move from cosmetic to enforced. B — two-sided cold-start, no tenancy in the schema, and its core sellable claim ("verified current endpoint evidence across counterparties") is exactly what the repo disclaims (`docs/claim-boundary.md:24-33`). C — weaker adoption pull (buyers often want blocking or nothing), same integration lift as A, and its central asset (the audit trail) is not tamper-evident today.

**Selected: Shape A**, because it is the only shape the repository's own control moment already targets end to end, and it reuses the most existing structure. **B rejected now** (speculative multi-party complexity atop an unbuilt evaluator; sells the disclaimed claim). **C rejected as the destination but adopted as the on-ramp:** the red-team is right that C is cheaper and lower-liability, and the existing `evidence_export` matches C more closely than a blocking gate — so the recommended sequence is *A's data model, entered in C's observe-only posture (advisory mode) first*, upgrading to enforcement only after the evaluator and demand are proven.

---

## 7. First sellable product (Shape A, advisory-first)

**Modules** (reuse vs. net-new):
1. Endpoint Profile — institution/legal-entity/endpoint/fallback CRUD. *Net-new CRUD over the existing table shape (`seed.py:39-75`).*
2. Pre-release Validation — rule evaluator producing check results from live field values. *Net-new; nothing computes today.*
3. Route Decision — verdict + explanation derived from check results. *Net-new; replaces hardcoded `route_decisions`.*
4. Fallback — preserve/select fiat SSI using existing columns. *Reuse (`seed.py:69-72`).*
5. Authority / four-eyes — enforced maker-checker with distinct authenticated actors. *Net-new; replaces cosmetic role selector.*
6. Evidence & Audit — append-only ledger + export, hardened for tamper-evidence. *Partial reuse (`app.py:171-186`) + hardening.*
7. Admin / Policy Config — per-institution thresholds. *Net-new; replaces the global 6-row policy list.*

**Core flow:** create/import profile → run validation → receive decision (approve/hold/block) with explanation → if blocked/held, fallback preserved/selected and repair task opened → four-eyes approval required to override or approve fallback use → evidence receipt emitted and appended → repair loop re-validates and re-decides.

**Decision states:** APPROVED, BLOCKED, HELD, plus a net-new RE-VALIDATED state — which requires dropping `UNIQUE` on `route_decisions.scenario_id` (`seed.py:117`) and adding a `previous_decision_id`, since the schema today cannot represent a second decision.

**Evidence outputs:** per-instruction JSON reusing the existing export shape (checks, decision, operator actions, audit events, boundary string), with a per-instance receipt identifier (fixing the scenario-invariant `SECT-SYN-2026-000184`), and hash-chaining added in the "later" phase.

**Admin/control surfaces:** policy-threshold config (per check: advisory vs. blocking, freshness window); user/role management for who may act as operator, reviewer, approver.

**Error/repair paths:** stale allowlist → repair → re-validate → approve; expired authority → hold → four-eyes → renew or override; incomplete payload → block → complete → re-validate; unknown `action_type` must become a `400` instead of silently degrading to a generic status (`app.py:130-136`).

**Explicit non-goals (v1):** no payment execution; no live wallet-ownership, LEI/vLEI, screening, or chain-analytics; no cross-institution registry (Shape B); no rails beyond tokenized-deposit + one fiat fallback; no public deployment.

---

## 8. Breadboard — core path

| Actor | Input | System action | Output / state | Data / API | Evidence | Failure state |
|---|---|---|---|---|---|---|
| Ops analyst | new instruction (wallet, BIC, LEI) | create endpoint profile | status = pre-release | `POST /api/endpoints` (**net-new**; today one fixed row, `seed.py:277-283`) | audit "Profile loaded" | missing field → `400`, no record |
| System | profile record | evaluate 6 checks from field values | `policy_checks` good/warn/bad | rule evaluator (**net-new**; today static, `seed.py:174-232`) | audit "Pre-validation started" + per-check events | unresolved upstream field → check = bad with reason; run still completes |
| System | check results | compute verdict + repair text | `route_decisions` row | decision engine (**net-new**; today hardcoded, `seed.py:183/203/223`) | audit "Policy decision" | undecidable → default HOLD, never silent approve |
| System | verdict BLOCKED/HELD | preserve/select fiat fallback | fallback selected/retained | `settlement_endpoints.fallback_*` (reuse, `seed.py:69-72`) | audit "Fallback retained/selected" | no fallback on file → escalate to hold (**net-new**; today fallback always "good") |
| Ops analyst | verdict BLOCKED | open repair task | `operator_actions` row | `POST /api/scenarios/{slug}/actions` (exists, `app.py:250-261`) | audit "Persisted operator action" (`app.py:154-159`) | unknown profile → `404` (`app.py:259`) |
| Risk reviewer | verdict HELD | review, renew vs. override | risk-queue status change | `GET /api/scenarios/{slug}` + review route (**net-new**) | audit "Risk task opened" | reviewer == requester → reject (**net-new**) |
| Four-eyes approver (distinct actor) | approve/override | record second-actor approval | approval recorded | actions route with actor-distinctness check (**net-new**; today none, `app.py:139-142`) | audit "Approval recorded" | approver == requester → `409` |
| Ops/risk/auditor | download request | generate evidence receipt | signed JSON bundle | `GET /api/evidence/{slug}` (exists, `app.py:171-186`) | `evidence_type=...db_export` | not found → `404` (`app.py:227`) |

---

## 9. Architecture path — demo baseline to product service

**BUILD-NOW (single-tenant, synthetic, localhost until a separate security/deployment review):**
- Rule/policy evaluator replacing the hardcoded `SCENARIOS` fixtures (`seed.py:172-233`).
- Endpoint-profile registry with lifecycle (draft/active/superseded) and CRUD; today only one row exists (`seed.py:277-283`) and there is no write route beyond actions.
- Authenticated identity and server-enforced roles; today `actor` is a forgeable client string (`app.py:141`) and there is no auth anywhere (`app.py:246-267`).
- A `tenant_id` column on core tables (isolation enforcement can be gated, but the column must land early to avoid a full migration later; none exists, `seed.py:36-160`).
- Tamper-evident evidence ledger (hash-chained or write-once) over today's ordinary `audit_events` (`seed.py:127-136`).
- Enforced four-eyes / maker-checker state machine with distinct approver identity.
- Structured request/error logging; today `log_message` is a no-op (`app.py:192-193`) while unhandled exceptions still leak raw tracebacks.
- Decision history: drop `UNIQUE` on `route_decisions.scenario_id` (`seed.py:117`) and add versioned decisions.

**GATED-FUTURE (each requires its own security, data, and deployment approval per `SECURITY.md:13` and `docs/product-spine.md:55`):**
- LEI/vLEI authority-verification adapter; custody / wallet-ownership-proof adapter; licensed screening adapter; chain-analytics adapter; Travel-Rule/beneficiary-data adapter; payment-rail execution adapter.
- TLS, secrets management, external SSO/OIDC; a real migration mechanism (today `seed.py:249-250` destroys and rebuilds the DB); non-localhost, tenant-isolated deployment; backup/retention.

**Authority boundary:** who may create/modify an endpoint profile must be distinct from who approves a release/override — a distinction the schema cannot currently express.

---

## 10. Trust and claim boundary

- **Claimable now (Tier 0 — synthetic pattern):** "demonstrates a control-workflow pattern using synthetic fixtures"; the endpoint-profile data-model shape; "the demo API exposes no update/delete routes" (append-only *at the API surface*, not "tamper-proof"); "evidence export mirrors displayed state." No overclaim exists in shipped docs (verified).
- **Claimable after MVP + pilot evidence (Tier 1 — partner sandbox data, still no live verification):** a real end-to-end workflow tested with a design partner's own test data and logged human sign-off. **Prerequisites before this tier is safe:** real auth/authz; tenancy; durable audit-integrity beyond "no delete route"; a documented retention/deletion policy; and a fix for the stored-injection surface. Still cannot claim live identity/authority, wallet ownership, screening, or production compliance.
- **Claimable only after real integrations/assurance (Tier 2 — each claim tied to a specific integration):** "live legal-entity/authority verification" needs an authoritative LEI/vLEI source; "live wallet ownership" needs custody attestation or proof-of-control; "live screening" needs a licensed provider plus match-review workflow; "chain analytics" needs a licensed vendor and data agreement; "payment execution" needs a real rail plus full review; "production compliance" is the deploying institution's own per-jurisdiction legal determination, which this review does not assert.

---

## 11. Commercial path

All items are hypotheses; the repo contains no customers, prices, or interviews.

- **Design partners (situation-typed, not named):** (1) a bank/PSP treasury-ops team already piloting tokenized deposits alongside fiat SSI rails with an existing dual-control culture; (2) a creditor/correspondent bank acting as a fallback SSI holder being asked to accept token instructions; (3) a digital-asset custodian wanting to show clients an auditable pre-release control layer; (4) a tokenized-deposit/stablecoin operator needing beneficiary-data completeness evidence; (5) a risk function wanting to run the checks in shadow mode to quantify failure rate.
- **Discovery questions (the decisive ones):** How many settlement instructions target a wallet endpoint today, and is the volume growing? When wallet/custody evidence is stale, how is it caught now? Of the six checks, **how many do you already cover elsewhere** (the wedge dies if the answer is "four of six")? Does a real "block token / fall back to fiat" fork actually occur in your flows today, or is the counterparty side not yet tokenized? Is your four-eyes on endpoint changes system-enforced or policy-only? Who can override a block, and is the override logged in an exportable way? If a control wrongly blocks a real settlement, whose SLA and budget absorb the delay?
- **Pilot shape:** 8–12 weeks, **advisory-mode-first** (flag-and-log, never auto-block), scoped to 1–2 corridors already in the partner's token pilot, using partner-controlled data in a separate environment; this repository stays synthetic. Success metrics: true-positive vs. false-positive/override rate; triage time per exception; audit-packet assembly time vs. baseline; fallback-continuity rate.
- **Land-and-expand:** land the single-relationship profile → validation → decision → evidence flow; expand along the data-model layers (institution → portfolio; authority → verified vLEI pipeline; endpoint → multi-rail/custodian; control → enforced blocking with real providers; trace → enterprise audit reporting).
- **Packaging/pricing hypotheses:** (A) per-endpoint-profile / per-entity subscription; (B) platform fee + usage per validation/decision event; (C) fixed advisory-mode pilot fee converting to an annual per-institution/per-corridor license. Each is a hypothesis to test against a partner's actual profile count, settlement volume, and budget cycle.
- **Buyer objections and the honest current answer:** *integration burden* — today zero live integrations, all ahead; *liability for a wrong block* — no false-positive rate or liability framework exists, which is why advisory-first is mandatory; *build-vs-buy* — the shape is ~1,150 lines and reproducible from the public docs, so value must come from verified data + enforced workflow + (later) network effects; *data licensing* — the repo imports zero external rows, so real BIC/LEI/screening/analytics licensing is a separate, unpriced dependency; *vendor risk* — self-declared not-for-public-exposure and enforced localhost-only.

---

## 12. Roadmap

**0–30 days — prove the product can decide.**
- Rule evaluator replaces hardcoded checks/decisions. *Exit gate:* evaluator reproduces the three existing verdicts exactly from field inputs (regression vs. `smoke.py:30-34`) **and** correctly classifies ≥2 new synthetic edge cases (missing fallback; simultaneous expired allowlist + authority) → binary 5/5.
- Endpoint-profile CRUD. *Exit gate:* ≥10 distinct synthetic profiles created via API, each independently and correctly evaluated.
- Server-enforced four-eyes (reject same-actor approval). *Exit gate:* automated test — identical-actor pair rejected, distinct-actor pair accepted, 100% pass.
- Cheap demand tests (no build): take the six-check list to 3–5 payment/treasury-ops contacts and ask "how many of these do you already cover?"; run one liability-ownership interview ("whose SLA absorbs a wrong block?"). *Exit gate:* documented answers from ≥3 contacts; a named owner (or a recorded "no owner," which argues for observe-only).

**31–90 days — make it trustworthy with one real seam.**
- Real authentication replacing no-auth/localhost-only. *Exit gate:* unauthenticated mutating request returns `401`; security-review sign-off obtained.
- Per-institution policy configuration. *Exit gate:* two synthetic institutions with different thresholds produce different verdicts for identical field values.
- One lowest-risk real integration seam end-to-end (e.g., an internal allowlist feed, not live LEI/vLEI or screening). *Exit gate:* ≥1 check computed from a live-fetched, review-approved feed; the check fails closed when the feed is unreachable.

**Later — gated on approval.**
- Tamper-evident audit trail (hash-chained/write-once). *Exit gate:* an attempted update/delete of a historical row is detected or rejected.
- Hosted, tenant-isolated deployment beyond localhost. *Exit gate:* a formal security/deployment sign-off exists; until then the product stays localhost-only.
- Evaluate (do not build) Shape-B registry extensions only once Shape A has a paying reference customer. *Exit gate:* a written go/no-go citing ≥1 production reference customer, with no registry code committed before that memo.

---

## 13. Risk register

| Class | Risk | Grounding | Current posture |
|---|---|---|---|
| Product | No decision engine; the "control tower" answers only three canned scenarios | `seed.py:172-233` ≡ `demo/index.html:475-539` | Highest-priority build; is the next gate |
| Adoption | Six checks may overlap ~4/6 with controls banks already own | `seed.py:163-170` (red-team) | Lead with the two new wallet/endpoint axes; test in discovery |
| Market | Cross-institution token-route volume may be pre-market (HYPOTHESIS) | not evidenced in-repo | Validate the "block/fallback fork exists today" question first |
| Regulatory | Blocking money movement creates false-positive/liability exposure; no owner assigned | verdict strings imply enforcement; no liability model | Advisory-first defers, not resolves; decision-box item |
| Data/licensing | Real BIC/LEI/screening/analytics data must be licensed; forward artifact-licensing uncertainty | `source_manifest.json` (rows = 0); `docs/claim-boundary.md:33` | Unpriced dependency; keep synthetic until scoped |
| Integration | A synchronous pre-release gate needs live feeds + auth + tenancy — the whole product | `source_manifest.json`; `app.py` (no auth); `seed.py` (no tenant) | Sequence behind the evaluator and one real seam |
| Security | Stored-injection via unescaped `action_type`; CSRF-reachable write (Content-Type ignored); no security headers | `app.py:140,151-153,256`; `demo-db/index.html:135`; verifier-confirmed | Bounded by localhost today; must fix before any exposure |
| Operational | No migrations (destructive reseed), no CI, no structured logging, weak concurrency, uncaught non-dict/non-numeric inputs | `seed.py:249-250`; no `.github/`; `app.py:192-193,255,264-267` | Demo-acceptable; product-blocking |
| Narrative | Cosmetic four-eyes can create false confidence in a pilot review — worse than absent | `demo/index.html:364,473`; `app.py:139-168` | Do not imply enforced dual-control until it is enforced |

---

## 14. Evidence-backed findings (all P0/P1 independently verified)

Scout status = raised by ≥1 scout; verifier verdict from the independent skeptic-verifier.

| Sev | Lens | Finding | Evidence (file:line) | Scout | Verifier | Gate affected |
|---|---|---|---|---|---|---|
| P0 | architecture / thesis | No policy/decision engine; checks and verdicts are hardcoded fixtures, never computed | `seed.py:172-233`; `app.py:82-123`; `demo/index.html:475-539` | raised | VERIFIED | 0–30 rule evaluator |
| P0 | roadmap / thesis | No endpoint CRUD; one shared `endpoint_id=1`; status columns literal `"scenario_driven"` | `seed.py:277-283,174/194/214,282`; routes `app.py:213-268` | raised | VERIFIED | 0–30 CRUD |
| P0 | safety / thesis | Four-eyes is cosmetic; server never checks actor identity; `actor` is forgeable client text | `demo/index.html:360-366,470-474,624`; `app.py:139-168,141` | raised | VERIFIED | 0–30 four-eyes |
| P0 | architecture / quality | No authentication on any route; localhost enforced only by a startup `SystemExit` guard | `app.py:246-267,276-277` | raised | VERIFIED | 31–90 auth |
| P0 | architecture | `route_decisions.scenario_id` is `UNIQUE`; schema cannot store decision history/recomputation | `seed.py:117` | raised | VERIFIED | 0–30 evaluator / decision history |
| P1 | demo-ux / architecture | Persisted operator actions never change decision/checks (no `UPDATE`/`DELETE` in `app.py`) | `app.py:139-168` | raised | VERIFIED | 0–30 repair loop |
| P1 | safety / quality | Stored-injection: unsanitized `action_type` rendered via unescaped `innerHTML`; CSP `unsafe-inline` does not mitigate | `app.py:140,151-153`; `demo-db/index.html:135,6` | raised | VERIFIED | Tier-1 security |
| P1 | quality / safety | CSRF-reachable write: no Origin/CSRF check; body parsed as JSON regardless of Content-Type (text/plain bypass) | `app.py:246-267,256` | raised | VERIFIED | Tier-1 security |
| P1 | architecture / roadmap | Audit trail not tamper-evident; append-only only by absence of mutating routes | `seed.py:127-136`; `app.py` | raised | VERIFIED | Later ledger |
| P1 | architecture / quality | No schema migration; seed destructively deletes and rebuilds the DB every run | `seed.py:249-250` | raised | VERIFIED | Later deploy |
| P1 | architecture / safety | No `tenant_id`/org scoping anywhere in the schema | `seed.py:36-160` | raised | VERIFIED | 31–90 tenancy column |
| P1 | quality | No CI; mandatory verification commands never auto-enforced | no `.github/`, no `*.yml`/`*.yaml` | raised | VERIFIED | 31–90 ops |
| P2 | demo-ux / thesis | Evidence receipt id is scenario-invariant; receipt download uses a fixed filename | `demo/index.html:385,575,674` | raised | VERIFIED | first-product evidence |
| P2 | demo-ux | `hold_payment` action exists server-side but has no UI control | `app.py:130-136`; `demo-db/index.html:61-62` | raised | VERIFIED | first-product UX |
| P2 | safety | `source_manifest` DB table is seeded but never queried; the "reference-manifest API" is file-backed | `seed.py:258-264`; `app.py:36-42,239-240` | raised | VERIFIED | doc/impl drift |
| P2 | safety / quality | `do_POST` catches only JSON/sqlite errors; a non-dict JSON body or non-numeric `Content-Length` crashes uncaught | `app.py:255,264-267,139` | raised | VERIFIED | Tier-1 robustness |
| Positive | safety / demo-ux | Claim discipline is real and test-enforced; no overclaim in shipped prose; `STATIC_QA PASS` | `demo/qa_static_demo.py:47-58`; `docs/claim-boundary.md`; `README.md:74-76` | raised | VERIFIED | trust boundary |

No P0 claim violation exists: no shipped document asserts live verification, certification, endorsement, payment execution, or production compliance; the localhost guardrail is enforced in code; and no real customer, payment, or credential data is present.

---

## 15. Raf decision box

Genuine strategy calls that the repository cannot answer:

1. **Advisory vs. blocking appetite.** Are we willing to carry the liability of an automated block/hold, or do we commit to advisory-mode-only until a named owner and SLA exist? This decides whether the destination is Shape A or a permanent Shape C.
2. **Wedge future.** Are we underwriting the internal gate (A) as the end state, or is A a beachhead toward a cross-counterparty registry (B)? B needs tenancy and a network from day one and sells the currently-disclaimed "verified evidence" claim.
3. **Which check becomes real first.** Given data-source access, do we make wallet-allowlist-freshness or authority-evidence the first computed, live-fed check? Lead with the axis that is genuinely new versus existing fiat controls.
4. **First design-partner profile.** Originating bank, digital-asset custodian, or creditor/fallback-SSI holder — each implies a different integration and a different first buyer.
5. **Build vs. partner for verification.** Do we build the LEI/vLEI, custody, and screening adapters, or partner for them? This is the difference between a control layer and a data-plus-control platform, and it is the main lever on defensibility.

---

## 16. Next single GO gate

**GATE-1 — "Compute the verdict."**

- **Exact scope:** in `demo-db/` only, replace the hardcoded per-scenario decision/check fixtures with a deterministic rule evaluator that derives all three existing verdicts (`TOKEN_ROUTE_BLOCKED_FIAT_FALLBACK_SELECTED`, `TOKEN_ROUTE_APPROVED_FIAT_FALLBACK_RETAINED`, `AUTHORITY_EXPIRED_MANUAL_HOLD`) from settlement-endpoint, authority, allowlist, and payload field values.
- **Acceptance evidence:** an extended smoke asserts 5/5 correct verdicts computed from field inputs — the three existing scenarios reproduced exactly (regression against `smoke.py:30-34` `EXPECTED_VERDICTS`) plus two new synthetic edge-case profiles (missing fallback on file; simultaneous expired allowlist and authority) classified correctly. Pair this build gate with the cheap demand test ("how many of the six checks do you already cover?") answered by ≥3 payment/treasury-ops contacts.
- **Closed side-effect gates preserved (must remain closed at GATE-1):** no authentication added; no non-localhost bind; no real customer/payment/provider data; no live identity/authority/wallet/screening/chain-analytics call; no payment execution; no hosted deployment, migration, restart of shared services, or public exposure; fixtures stay synthetic; changes confined to the `demo-db/` scaffold.

If GATE-1 succeeds and the demand test shows a genuine gap (not "four of six already covered"), proceed to the 31–90 day work. If the demand test shows heavy overlap or no liability owner surfaces, flip to observe-only (Shape C) or STOP — do not build the enforcement path on an unvalidated wedge.
