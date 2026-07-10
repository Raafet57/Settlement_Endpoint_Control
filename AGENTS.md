# Agent instructions

## Product boundaries

- Keep all fixtures synthetic.
- Do not add real customer, account, wallet, credential, or payment data.
- Do not claim live verification, payment execution, certification, endorsement, production compliance, or production readiness without verified evidence.
- Keep the static demo self-contained: no telemetry, remote assets, third-party scripts, or backend calls.
- Keep the SQLite application localhost-only unless a separate security and deployment review approves otherwise.

## Coding discipline

- **Think before coding:** state assumptions, ambiguity, and trade-offs before non-trivial changes.
- **Simplicity first:** implement the minimum solution; avoid speculative abstractions and features.
- **Surgical changes:** touch only what the task requires; no drive-by refactors.
- **Goal-driven execution:** define verifiable success criteria and run the relevant checks.
- Preserve existing style and remove only dead code introduced by the current change.
- Never claim a check passed unless its command actually ran successfully.

## Verification

```bash
python3 demo/qa_static_demo.py
python3 demo-db/smoke.py
```

Run `python3 demo/qa_browser_mobile.py` when Playwright and Chromium are available.
