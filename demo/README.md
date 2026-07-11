# Public landing page and static reference demo

Self-contained public page for Settlement Endpoint Control Tower: a full
landing narrative plus the embedded browser demo console.

## What it covers

- The settlement-endpoint problem: token endpoints handled as pasted addresses
  instead of governed settlement instructions.
- The control moment: valid beneficiary and usable fiat SSI, but a token
  endpoint without current control evidence — blocked, fallback selected,
  evidence recorded.
- The interactive demo console: endpoint profile, pre-validation, route
  decision, and evidence/audit views; blocked, approved, and authority-expired
  scenarios; operations analyst, risk reviewer, and four-eyes approver
  perspectives; client-side synthetic evidence receipt.
- The working localhost reference application (`demo-db/`): deterministic
  evaluator, endpoint profile lifecycle, repair and evidence refresh,
  deterministic revalidation, and append-only decision history through the
  localhost application API (linked decision versions, prior versions
  preserved) — described as localhost-only synthetic operational-depth proof,
  never exposed by this page.
- The claim boundary: what the demo is and is not.

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

The page is synthetic and browser-only. It makes no external network calls and
performs no live payment, identity, authority, wallet, screening, or
compliance operation.
