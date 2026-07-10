# Claim boundary

## Implemented

- Static browser application with four operating views.
- Three deterministic synthetic scenarios.
- Three role views.
- Client-side synthetic evidence export.
- Local SQLite schema v3 with deterministic seed.
- Local scenario, health, audit, action, evidence, and reference-manifest APIs.
- Persisted synthetic operator action and appended audit event.
- DB-backed synthetic evidence export.

## Synthetic or represented

- Institution and BIC-shaped identifiers.
- LEI and verifiable-authority-style summaries.
- Wallet endpoint, custody, and controller states.
- Beneficiary-data completeness and policy checks.
- Fiat fallback instructions.
- ISO 20022/UETR-style trace events.
- Public reference descriptors; no external source rows are imported.

## Not implemented or claimed

- Real payment execution.
- Live payment-network or platform integration.
- Live legal-entity or authority verification.
- Live wallet ownership proof.
- Live sanctions or chain analytics.
- Production compliance decisions.
- Real customer, account, wallet, or payment data.
- Proprietary reference-data rows.
- Public deployment of the SQLite application.

Use `synthetic reference demo`, `control pattern`, `represented`, and `style` wording. Do not describe this repository as certified, endorsed, fully compliant, or production-ready.
