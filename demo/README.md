# Static reference demo

Self-contained browser demo for Settlement Endpoint Control Tower.

## What it demonstrates

- Identity-bound settlement endpoint profile.
- BIC, LEI/vLEI-style authority, wallet endpoint, custody context, token rail, and fiat fallback.
- Blocked, approved, and authority-expired scenarios.
- Operations analyst, risk reviewer, and four-eyes approver views.
- Endpoint pre-validation, route decision, audit trail, and synthetic evidence receipt.

## Run locally

From the repository root:

```bash
python3 -m http.server 8000 --bind 127.0.0.1 --directory demo
```

Open `http://127.0.0.1:8000/`.

## Verify

```bash
python3 demo/qa_static_demo.py
```

The demo is synthetic and browser-only. It makes no external network calls and performs no live payment, identity, authority, wallet, screening, or compliance operation.
