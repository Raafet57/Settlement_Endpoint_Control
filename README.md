# Settlement Endpoint Control Tower

A synthetic reference product for pre-validating identity-bound settlement endpoints before tokenized or fiat value moves.

## Product thesis

Digital money should not scale on pasted wallet addresses. Financial institutions need governed settlement endpoint profiles that bind destination coordinates to institution identity, legal-entity authority, custody context, policy controls, fallback instructions, and an evidence trail.

## Memorable control moment

The beneficiary and fiat standing settlement instruction are valid, but the requested tokenized endpoint is unsafe or incomplete. The control tower blocks the token route before release, explains the missing control evidence, preserves the fiat fallback, and emits an audit-ready receipt.

## What is included

### Browser-only reference demo

`demo/` contains a self-contained static application with:

- four operating views: endpoint profile, pre-validation, route decision, and evidence/audit;
- three deterministic scenarios: blocked endpoint, refreshed endpoint approved, and expired authority evidence;
- three role views: operations analyst, risk reviewer, and four-eyes approver;
- client-side synthetic evidence export;
- no backend calls, telemetry, third-party scripts, or remote assets.

### Local SQLite workflow proof

`demo-db/` contains a localhost-only standard-library Python application with:

- deterministic schema v3 seed;
- scenario, health, audit, and reference-manifest APIs;
- persisted synthetic operator actions;
- audit-event append and readback;
- DB-backed evidence export.

The SQLite path is local operational-depth evidence, not a public backend.

## Quick start

Static checks:

```bash
python3 demo/qa_static_demo.py
python3 -m http.server 8000 --bind 127.0.0.1 --directory demo
```

Open `http://127.0.0.1:8000/`.

SQLite workflow:

```bash
python3 demo-db/seed.py
python3 demo-db/smoke.py
python3 demo-db/app.py --host 127.0.0.1 --port 4188
```

Open `http://127.0.0.1:4188/`.

Optional browser/mobile verification requires Python Playwright and an installed Chromium runtime:

```bash
python3 demo/qa_browser_mobile.py
```

## Repository layout

```text
demo/                 self-contained static reference demo
demo-db/              localhost-only SQLite workflow proof
docs/product-spine.md product model and control flow
docs/claim-boundary.md implemented, synthetic, and unproven boundaries
SECURITY.md            security and disclosure posture
```

## Claim boundary

This repository demonstrates a control pattern using synthetic fixtures. It does not execute payments or perform live identity, authority, wallet-ownership, sanctions, chain-analytics, or production compliance checks. It includes no real customer/payment data and no proprietary reference-data rows.
