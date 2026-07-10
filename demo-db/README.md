# Local SQLite workflow proof

Localhost-only SQLite application for Settlement Endpoint Control Tower.

## Boundary

- Synthetic fixtures only.
- No secrets or credentials.
- No private, customer, or production data.
- No external network calls.
- Localhost bind only.
- Not approved as a public backend.
- No live payment execution or verification; all decisions are advisory only.
- Not production-ready and not a compliance certification.

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

The current schema version is **6**. The forward-only, nondestructive `4 -> 5`
step (`endpoint_profiles_registry`) adds the `endpoint_profiles` registry table
and backfills one `active` profile per existing settlement endpoint. The
additive `5 -> 6` step (`endpoint_repair_decisions`) then adds the three SEC-P30
tables — `profile_decisions`, `repair_tasks`, `repair_events` — and the one-open
partial UNIQUE index as empty tables; it creates no rows and alters or drops no
existing business row, so every prior profile, operator action, and audit event
is preserved byte-for-byte. A fresh `seed.py` is born at the same v6 state with
the same ledger. Beyond the exact declared v6 table shapes, a database claiming
v6 must also carry consistent decision/repair/event **rows**: every row is scoped
to the server-owned tenant, each profile's decisions form a contiguous
`1..N` baseline-then-revalidation chain, each repair task and its event trail
cohere with that chain, and every decision/task `profile_id` resolves to an
`endpoint_profiles` row under the same tenant — otherwise the database fails
closed as inconsistent. The version is tracked authoritatively in `PRAGMA user_version`,
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

POST /api/endpoint-profiles/{id}/repair              open a repair (records baseline decision)
POST /api/endpoint-profiles/{id}/repair/evidence     persist refreshed evidence
POST /api/endpoint-profiles/{id}/repair/revalidation revalidate (records superseding decision)
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

## Repair and revalidation (SEC-P30)

An **active** profile whose latest advisory decision is a hold (blocked from
automated release) can be carried through a narrow, fixed repair workflow that
layers **versioned advisory decisions** over the profile without mutating it.

**State machine** — the only legal step order, one live repair at a time:

```text
   open ----------------> evidence_refreshed ----------------> resolved
 (records baseline      (persists refreshed authority /       (records the
  decision v1 for        allowlist / payload evidence          superseding
  the failing profile)   onto the repair task)                 decision vN+1)
```

- **Open** records the profile's current failing evaluation as the immutable
  baseline decision (`origin=baseline`, `version=1`, no predecessor); a later
  cycle reuses the latest recorded decision as its baseline. An already-approved
  latest decision has `nothing_to_repair`; a second live open is
  `repair_already_open`.
- **Evidence refresh** persists the refreshed `authority` / `allowlist` /
  `payload` values onto the repair task only. Unsupported enums, unknown, or
  missing fields fail closed (`422`) with no write.
- **Revalidation** computes a **superseding** decision (`origin=revalidation`,
  `version=N+1`) **solely from the refreshed persisted evidence** via
  `evaluator.evaluate`, links it back to the repaired baseline, and resolves the
  task to it. The verdict is deterministic: the same refreshed evidence always
  produces the same advisory verdict (ALLOW or a specific HOLD).

**Immutable decision snapshots / version links** — each `profile_decisions` row
snapshots the graded evidence that produced its verdict and is never updated. A
profile's decisions form a linear `1..N` chain (`previous_decision_id` links each
revalidation to the decision it supersedes); every prior decision and its
evidence snapshot stay queryable after a repair.

**Narrow repair-event trail** — `repair_events` appends exactly one immutable,
ordered event per step (`repair_opened`, `evidence_refreshed`, `revalidated`),
profile- and task-scoped, carrying only the step kind, the accountable actor, and
the decision the step concerns (none for the evidence step). No request/response
payload is stored. It is a profile trail and never carries a scenario id.

**Promoted advisory verdict on reads** — once a repair records a decision, the read
surfaces promote the **latest versioned decision**: the collection
(`GET /api/endpoint-profiles`) reports its verdict in `verdict`, and the detail
exposes it as an explicit `latest_decision` (and a promoted `verdict`). The
intrinsic `evaluation` — computed live from the immutable constituent fields — stays
separately available and is never rewritten, so a HOLD profile revalidated to ALLOW
reads ALLOW on the advisory surface while its intrinsic evaluation still reflects the
unchanged fields. Each mutation receipt and each compound read is assembled from a
single connection snapshot / exact ids, so concurrent repair cycles can never mix
generations into a response.

**Lifecycle interleaving** — a profile with a live (non-resolved) repair cannot be
superseded (`409` `repair_in_progress`), and the evidence-refresh and revalidation
steps re-check `lifecycle_state = active` inside their own `BEGIN IMMEDIATE`
transaction and fail closed otherwise. The two directions therefore cannot
interleave to strand a live repair on a superseded profile; a completed (resolved)
repair leaves no live task, so supersession is then permitted and the resolved
history stays queryable on the superseded profile.

**Invalid transition / replay** — every out-of-order, replayed, or cross-profile
step fails closed transactionally (`409` `invalid_transition`, `no_open_repair`,
`invalid_repair_state`, `repair_already_open`) with no partial write; the
database's per-row state CHECKs and the one-open partial UNIQUE index enforce the
same rails so a half-written or duplicate-live task cannot exist.

**Active-profile immutability** — the repair is an advisory overlay: the active
profile's constituent institution / legal-entity / endpoint / fallback rows and
its intrinsic evaluation are never changed (SEC-P20 immutability is preserved).
The refreshed evidence lives on the repair task and the revalidation decision,
not on the profile.

Every repair route reuses the same localhost write boundary as the other profile
writes (strict `application/json`, loopback Origin/Host, bounded body, fail-closed
framing) and the same server-owned synthetic tenant.

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
