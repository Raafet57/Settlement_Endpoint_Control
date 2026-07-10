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

The current schema version is **5**. The forward-only, nondestructive `4 -> 5`
step (`endpoint_profiles_registry`) adds the `endpoint_profiles` registry table
and backfills one `active` profile per existing settlement endpoint, preserving
every existing business row; a fresh `seed.py` is born at the same v5 state with
the same ledger. The version is tracked authoritatively in `PRAGMA user_version`,
mirrored in `seed_meta.schema_version`, with applied steps recorded in the
`schema_migrations` ledger (validated as an ordered prefix at every step).
Unknown, missing, newer, or inconsistent version markers fail closed with a
stable error rather than being guessed; a database whose markers claim a version
but whose `schema_migrations` ledger is missing or does not match is rejected the
same way. The one supported legacy exception is the generated v3 state
(`seed_meta.schema_version=3` with `PRAGMA user_version=0`). A failed migration
**step** rolls back atomically and leaves data and version markers at the last
successfully committed version (for example, a failed `4 -> 5` step remains at
v4). There is no automatic migration on app start.

## APIs

```text
GET  /readyz
GET  /api/scenarios
GET  /api/scenarios/{slug}
POST /api/scenarios/{slug}/actions
GET  /api/evidence/{slug}
GET  /api/audit/counts
GET  /api/source-manifest

GET  /api/endpoint-profiles                       list profiles (state + verdict)
POST /api/endpoint-profiles                       create a draft profile
GET  /api/endpoint-profiles/{id}                  read one profile + live evaluation
PUT  /api/endpoint-profiles/{id}                  update a draft profile
POST /api/endpoint-profiles/{id}/activation       draft -> active
POST /api/endpoint-profiles/{id}/supersession     active -> superseded (atomic)
```

## Endpoint profiles

First-class, multi-instance **synthetic** endpoint profiles with a controlled
lifecycle. A profile is the aggregate record: it owns its lifecycle state and a
server-owned tenant, and links to the constituent `institutions`,
`legal_entities`, and `settlement_endpoints` rows that a create persists. It
wraps the original single shared endpoint fixture rather than replacing it: the
three shipped scenarios still share endpoint 1 (which the migration backfills as
one active profile), and the registry adds further independent profiles
alongside it.

**Lifecycle vocabulary** — exactly `draft`, `active`, `superseded`, used
identically across schema, API, UI, and docs.

**State machine** — the only legal transitions:

```text
        create                 activate                supersede (atomic)
   (none) ------> draft --------------------> active --------------------> superseded
                    ^                                                          |
                    | update (draft only)                       replacement draft
                    |                                            is activated in the
                  (immutable once active/superseded)            same transaction
```

- Create always yields a `draft`; there is no direct create-as-active.
- Update is allowed **only** while a profile is `draft`; active and superseded
  profile data is immutable through this API.
- Activation moves a `draft` to `active`.
- Supersession is one atomic replacement: an `active` profile becomes
  `superseded` (linked to its replacement via `superseded_by`) and a **draft**
  replacement becomes `active`. There is no transition out of `superseded`.
- There is **no delete route** — supersession preserves the old profile and all
  of its scenario, decision, audit, action, and source-lineage history. `DELETE`
  on a profile answers `405 method_not_allowed`.

**Request / response** — a create/update body is the exact nested shape
`{institution, legal_entity, endpoint, fallback}` (see `index.html` for the
fields). Read/create/update responses expose the constituent rows plus a live
`evaluation` (the six checks and the route decision), computed **per profile**
from its persisted institution / legal-entity / endpoint / fallback fields via
`evaluator.evaluate` — never from a scenario slug — so each profile is evaluated
independently. Supersession returns the superseded profile and its now-active
replacement.

**Validation** — the create/update boundary validates the exact nested shape,
required non-empty bounded strings, supported enum values
(`authority_status ∈ {current, expiring_soon, expired}`,
`allowlist_status ∈ {current, stale}`,
`endpoint_payload_status ∈ {complete, incomplete}`), and `bic` / `lei` / `uetr`
uniqueness, without coercion. Unknown or extra keys are rejected (not silently
ignored), and every rejection fails closed with a stable error code and no
partial write. Numeric ids parse strictly. Every profile write reuses the same
localhost boundary as the action route: strict `application/json`, loopback
Origin/Host, fail-closed framing, bounded body, and RFC JSON.

**Tenant** — every profile row carries a single server-owned constant tenant
(`synthetic-demo`); it is never client-supplied (a client-sent `tenant_id` is an
unknown key and is rejected), and every read/write/transition query is scoped
through it. This is a tenant-ready extension point only — there is no auth or
tenant infrastructure.

**Source lineage** — server-owned synthetic metadata is written for every created
constituent row and returned parsed, exactly as the existing APIs do.

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
