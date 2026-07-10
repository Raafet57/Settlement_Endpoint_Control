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
seed.py               current schema and deterministic synthetic seed (destructive reset)
migrate.py            nondestructive, versioned in-place migration for persistent local state
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

## Schema versions and migrations

Two explicit, separate commands manage local SQLite state:

- `seed.py` is **destructive**: it deletes and rebuilds `data/demo.sqlite` as a
  deterministic synthetic fixture at the current schema version. Use it for tests
  and demo reset.
- `migrate.py` is **nondestructive**: it applies ordered, versioned forward
  migrations to an existing persistent `data/demo.sqlite` in place, without
  deleting or rewriting existing operator actions or audit events.

```bash
python3 demo-db/migrate.py            # migrate the local DB in place
python3 demo-db/migrate.py --db PATH  # migrate a specific synthetic DB
```

The current version is tracked authoritatively in `PRAGMA user_version`, mirrored
in `seed_meta.schema_version`, with applied steps recorded in the
`schema_migrations` ledger. Unknown, missing, newer, or inconsistent version
markers fail closed with a stable error rather than being guessed; a database
whose markers claim the current version but whose `schema_migrations` ledger is
missing or does not match is rejected the same way. The one
supported legacy exception is the generated v3 state
(`seed_meta.schema_version=3` with `PRAGMA user_version=0`). A failed migration
rolls back atomically and leaves data and version markers unchanged. There is no
automatic migration on app start.

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

For the full project checks (this smoke plus static QA, the complete `demo-db`
unit suite, and Python/JSON/HTML/secret/retired-framing/workflow validation) run
the one canonical quality gate from the repository root — the same command CI
runs on Python 3.9:

```bash
python3 ci.py
```

## Local logging

`app.py` writes structured JSON-Lines request and error events to **stderr**
(the `SERVING` readiness banner stays on stdout). Each event carries only a
small allowlist of non-sensitive metadata — timestamp, level/event, HTTP method,
a normalized route label, numeric status, and elapsed time — and error events
add a stable `category` plus the exception class name. Request bodies, response
payloads, query strings, header values, actor/action values, SQL, and exception
text are never logged, and server error responses expose no raw exception detail.
