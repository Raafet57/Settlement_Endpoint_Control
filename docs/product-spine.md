# Product spine

## Product name

Settlement Endpoint Control Tower

## Thesis

Digital money introduces destination coordinates beyond classic account instructions. Wallet and token endpoints should be governed as settlement instructions, not handled as pasted addresses.

Before value release, an institution should be able to answer:

- Which institution and legal entity own or control the endpoint?
- Who has authority to maintain it?
- Which wallet, custody, network, and token context applies?
- Is the required beneficiary and Travel Rule context complete?
- Does policy allow this route now?
- Which fiat fallback remains available?
- What evidence explains the decision?

## Product object

An identity-bound settlement endpoint profile combines:

1. **Institution layer** — BIC, institution role, jurisdiction, and reachability posture.
2. **Legal-entity layer** — LEI and verifiable-authority-style evidence.
3. **Digital endpoint layer** — wallet, network, token, custody, controller, and allowlist status.
4. **Fallback layer** — fiat standing settlement instruction and intermediary context.
5. **Control layer** — freshness, authority, beneficiary-data, custody, risk, and policy checks.
6. **Trace layer** — route verdict, repair reason, operator actions, audit events, and ISO 20022/UETR-style evidence.

## Demo flow

```text
endpoint profile
→ pre-validation checks
→ token route approved, blocked, or held
→ fiat fallback preserved or selected
→ evidence receipt and audit trail
```

## Memorable moment

The beneficiary and fiat instruction are valid, but the requested tokenized endpoint lacks current control evidence. The control tower blocks token release, explains the evidence gap, preserves the fallback route, and creates an auditable decision receipt.

## Current implementation

- Self-contained browser demo with three scenarios and three roles.
- Localhost-only SQLite workflow with deterministic fixtures.
- Persisted synthetic operator actions and audit append.
- Client-side and DB-backed evidence exports.

## Expansion gates

Live integrations, production data, authentication, hosted persistence, screening providers, authority verification, wallet ownership proof, and payment execution require separate architecture, security, data, and deployment approval.
