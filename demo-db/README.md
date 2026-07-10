# Local SQLite workflow proof

Localhost-only SQLite application for Settlement Endpoint Control Tower.

## Boundary

- Synthetic fixtures only.
- No secrets or credentials.
- No private, customer, or production data.
- No external network calls.
- Localhost bind only.
- Not approved as a public backend.

## Files

```text
source_manifest.json  public reference descriptors; no imported source rows
seed.py               schema v3 and deterministic synthetic seed
app.py                localhost-only HTTP API/UI
index.html             UI backed by the local API and SQLite
smoke.py              persistence, evidence, and boundary smoke
qa_db_smoke.py         compatibility entry point for smoke.py
data/demo.sqlite       generated locally and ignored by git
```

## Run

From the repository root:

```bash
python3 demo-db/seed.py
python3 demo-db/app.py --host 127.0.0.1 --port 4188
```

Open `http://127.0.0.1:4188/`.

## APIs

```text
GET  /readyz
GET  /api/scenarios
GET  /api/scenarios/{slug}
POST /api/scenarios/{slug}/actions
GET  /api/evidence/{slug}
GET  /api/audit/counts
GET  /api/source-manifest
```

## Verify

```bash
python3 demo-db/smoke.py
```

The smoke reseeds a disposable synthetic state, writes one operator action, verifies persistence and audit append, and validates the evidence export.
