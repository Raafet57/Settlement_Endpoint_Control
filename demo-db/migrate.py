#!/usr/bin/env python3
"""Nondestructive, versioned SQLite migration foundation for the localhost demo.

This is the separate, explicit migration command for *persistent* local synthetic
state. It is deliberately distinct from ``seed.py``, which stays the destructive
deterministic fresh-fixture command (it deletes and rebuilds ``data/demo.sqlite``).
``migrate.py`` never deletes the database and never rewrites business rows: it
applies ordered forward migrations in place, preserving existing operator actions
and audit events.

Boundary: synthetic fixtures only, localhost only. No external network calls, no
telemetry, no remote assets, no automatic app-start migration. Stdlib only.

Versioning model
----------------
* ``PRAGMA user_version`` is the authoritative schema-version marker going forward
  and is mirrored in ``seed_meta.schema_version`` for the existing readers.
* Applied foundation steps are recorded in the ``schema_migrations`` ledger.
* Version resolution fails closed: unknown, missing, newer, or inconsistent
  markers raise rather than being guessed. The single supported legacy exception
  is the real generated v3 state ``seed_meta.schema_version=3`` with
  ``PRAGMA user_version=0`` (the schema this codebase shipped never set
  ``user_version``).
* Each migration step runs inside one transaction; on failure the step is rolled
  back atomically (data, version markers, and schema all revert) and a stable
  ``MigrationError`` is raised.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "demo.sqlite"

# The schema version this codebase currently targets. A fresh seed and a fully
# migrated persistent database both end here.
CURRENT_SCHEMA_VERSION = 6
# The pre-foundation generated schema (seed_meta.schema_version=3, user_version=0).
BASELINE_SCHEMA_VERSION = 3

# The single server-owned synthetic tenant. It is the tenant-ready extension
# point for the endpoint-profile registry; it is never client-supplied and no
# auth/tenant infrastructure is built on top of it. app.py mirrors this value.
SYNTHETIC_TENANT_ID = "synthetic-demo"

# The graded-evidence enum domains the refreshed_* repair columns must draw from.
# The v6 DDL constrains only their presence per state (not their value set), so
# current-row validation pins the value domain here. Mirrors the evaluator's
# supported inputs in app.py; kept local to avoid a migrate<->app import cycle.
REFRESHED_AUTHORITY_STATES = ("current", "expiring_soon", "expired")
REFRESHED_ALLOWLIST_STATES = ("current", "stale")
REFRESHED_PAYLOAD_STATES = ("complete", "incomplete")
# Synthetic actor identities accepted by the localhost API. Current-row
# validation mirrors that boundary so a matching-but-empty/unknown task/event
# actor cannot masquerade as accountable provenance.
REPAIR_ACTORS = ("demo_operator", "ops_analyst")
AUDIT_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The foundation step (3 -> 4): introduce the migration ledger + explicit version
# markers. Metadata only; no business table is created, altered, or dropped.
FOUNDATION_MIGRATION_NAME = "foundation_schema_migrations_ledger"
# The profile-registry step (4 -> 5): add the endpoint_profiles registry table
# and backfill one active profile per existing settlement endpoint. Additive
# only; no existing business row is altered or dropped.
PROFILES_MIGRATION_NAME = "endpoint_profiles_registry"
# The repair/decision step (5 -> 6): add the profile_decisions ledger,
# repair_tasks workflow, and repair_events audit trail. Additive only -- three
# empty tables; no existing business row is created, read for mutation, altered,
# or dropped.
REPAIR_DECISIONS_MIGRATION_NAME = "endpoint_repair_decisions"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

# The endpoint-profile registry. A profile is the first-class, multi-instance
# aggregate: it owns its lifecycle state and a server-owned tenant, and links to
# an existing settlement_endpoint (the constituent record). ``superseded_by``
# records the atomic replacement that superseded this profile; it is UNIQUE so
# replacement history stays one-to-one (a replacement supersedes at most one
# profile -- no reuse/branching). The lifecycle vocabulary is exactly
# draft / active / superseded.
ENDPOINT_PROFILES_DDL = """
CREATE TABLE IF NOT EXISTS endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('draft','active','superseded')),
    superseded_by INTEGER UNIQUE REFERENCES endpoint_profiles(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (lifecycle_state IN ('draft','active') AND superseded_by IS NULL)
        OR (lifecycle_state = 'superseded' AND superseded_by IS NOT NULL AND superseded_by <> id)
    )
);
"""

# The columns a well-formed endpoint_profiles registry must carry at the current
# schema version. Introspected (not overbuilt) by _validate_current_schema.
REQUIRED_PROFILE_COLUMNS = (
    "id", "tenant_id", "endpoint_id", "lifecycle_state", "superseded_by", "created_at", "updated_at",
)

# The append-only, versioned advisory-decision ledger (SEC-P30). Each row records
# one computed route decision for a profile with an immutable evidence snapshot.
# ``version`` counts 1..N per profile; ``previous_decision_id`` is the backward
# linkage to the decision this one supersedes (UNIQUE so a predecessor is
# superseded by at most one successor -- a linear chain, no branching/reuse). A
# v1 decision is the ``baseline`` recorded when a repair opens (no predecessor); a
# later decision is a ``revalidation`` (must carry a predecessor). The evidence_*
# columns snapshot the graded inputs that produced the verdict; combined with the
# immutable profile constituents they fully determine it. Rows are never updated.
PROFILE_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS profile_decisions (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    profile_id INTEGER NOT NULL REFERENCES endpoint_profiles(id),
    version INTEGER NOT NULL,
    previous_decision_id INTEGER UNIQUE REFERENCES profile_decisions(id),
    origin TEXT NOT NULL CHECK(origin IN ('baseline','revalidation')),
    verdict TEXT NOT NULL,
    token_class TEXT NOT NULL,
    fiat_class TEXT NOT NULL,
    token_text TEXT NOT NULL,
    fiat_text TEXT NOT NULL,
    repair_text TEXT NOT NULL,
    evidence_authority_status TEXT NOT NULL,
    evidence_allowlist_status TEXT NOT NULL,
    evidence_payload_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(profile_id, version),
    CHECK(version >= 1),
    CHECK(
        (version = 1 AND origin = 'baseline' AND previous_decision_id IS NULL)
        OR (version > 1 AND origin = 'revalidation' AND previous_decision_id IS NOT NULL AND previous_decision_id <> id)
    )
);
"""

# The narrow repair workflow (SEC-P30). One row per repair cycle drives a fixed
# state machine open -> evidence_refreshed -> resolved. ``opened_decision_id`` is
# the (failing) baseline decision being repaired; ``resolved_decision_id`` is the
# superseding revalidation decision (UNIQUE: a decision resolves at most one
# task). The refreshed_* columns persist the operator-supplied evidence, and the
# three timestamps record the ordered action/evidence/decision sequence. The CHECK
# ties each state to exactly which fields are set, so a half-written row cannot
# exist. The partial UNIQUE index below allows at most one non-resolved task per
# profile, so a replayed or concurrent open fails closed at the database.
REPAIR_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS repair_tasks (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    profile_id INTEGER NOT NULL REFERENCES endpoint_profiles(id),
    actor TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','evidence_refreshed','resolved')),
    opened_decision_id INTEGER NOT NULL REFERENCES profile_decisions(id),
    resolved_decision_id INTEGER UNIQUE REFERENCES profile_decisions(id),
    refreshed_authority_status TEXT,
    refreshed_allowlist_status TEXT,
    refreshed_payload_status TEXT,
    created_at TEXT NOT NULL,
    evidence_refreshed_at TEXT,
    resolved_at TEXT,
    CHECK(
        (state = 'open'
            AND refreshed_authority_status IS NULL AND refreshed_allowlist_status IS NULL
            AND refreshed_payload_status IS NULL AND evidence_refreshed_at IS NULL
            AND resolved_decision_id IS NULL AND resolved_at IS NULL)
        OR (state = 'evidence_refreshed'
            AND refreshed_authority_status IS NOT NULL AND refreshed_allowlist_status IS NOT NULL
            AND refreshed_payload_status IS NOT NULL AND evidence_refreshed_at IS NOT NULL
            AND resolved_decision_id IS NULL AND resolved_at IS NULL)
        OR (state = 'resolved'
            AND refreshed_authority_status IS NOT NULL AND refreshed_allowlist_status IS NOT NULL
            AND refreshed_payload_status IS NOT NULL AND evidence_refreshed_at IS NOT NULL
            AND resolved_decision_id IS NOT NULL AND resolved_at IS NOT NULL)
    )
);
"""

# At most one live (non-resolved) repair task per profile. A partial UNIQUE index
# is the transactional guard against a replayed or concurrent second open.
REPAIR_TASKS_OPEN_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS repair_tasks_one_open_per_profile "
    "ON repair_tasks(profile_id) WHERE state <> 'resolved';"
)

# The append-only profile-repair event trail (SEC-P30). Exactly one row is
# appended per repair state step -- open, evidence refresh, revalidation -- so the
# workflow leaves an ordered, immutable audit sequence the mutable repair_tasks row
# cannot itself provide. Narrow and profile/task scoped: it records the step kind,
# the accountable actor, and the decision the step concerns (the baseline for the
# open, the superseding decision for the revalidation, none for the evidence
# refresh). No request/response payload is stored. ``sequence`` is 1/2/3 within a
# task; UNIQUE(task_id, sequence) and UNIQUE(task_id, event_type) make each step
# appendable exactly once, and the CHECK binds sequence, event_type, and
# decision-presence together so a malformed step row cannot exist. It never carries
# a scenario_id: profile events are not scenario events.
REPAIR_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS repair_events (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    profile_id INTEGER NOT NULL REFERENCES endpoint_profiles(id),
    task_id INTEGER NOT NULL REFERENCES repair_tasks(id),
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('repair_opened','evidence_refreshed','revalidated')),
    actor TEXT NOT NULL,
    decision_id INTEGER REFERENCES profile_decisions(id),
    created_at TEXT NOT NULL,
    UNIQUE(task_id, sequence),
    UNIQUE(task_id, event_type),
    CHECK(
        (sequence = 1 AND event_type = 'repair_opened' AND decision_id IS NOT NULL)
        OR (sequence = 2 AND event_type = 'evidence_refreshed' AND decision_id IS NULL)
        OR (sequence = 3 AND event_type = 'revalidated' AND decision_id IS NOT NULL)
    )
);
"""


def _lower_outside_string_literals(sql: str) -> str:
    """Lower-case SQL keywords/identifiers but keep single-quoted string literal
    content (and escaped '' quotes) in their exact declared case.

    Case-folding the whole statement would let a re-cased CHECK literal (e.g.
    'draft' -> 'DRAFT') fingerprint identically to the declared DDL while normal
    lower-case writes then fail the constraint. Bounded single-quote scan; not a
    general SQL parser.
    """
    out = []
    i, n = 0, len(sql)
    in_literal = False
    while i < n:
        ch = sql[i]
        if in_literal:
            out.append(ch)  # preserve literal content case verbatim
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":  # doubled '' escapes a quote: still inside
                    out.append("'")
                    i += 2
                    continue
                in_literal = False
            i += 1
        elif ch == "'":
            in_literal = True
            out.append(ch)
            i += 1
        else:
            out.append(ch.lower())
            i += 1
    return "".join(out)


def _canonical_table_ddl(sql: str) -> str:
    """Whitespace/case-normalized CREATE TABLE text for an exact-shape fingerprint.

    SQLite stores each CREATE statement verbatim (minus the trailing ';'), so
    collapsing whitespace, case-normalizing keywords/identifiers (NOT quoted
    literal content), and dropping the optional IF NOT EXISTS yields a stable
    fingerprint to compare a stored table against the declared DDL. Bounded
    normalizer, not a general SQL parser.
    """
    cased = _lower_outside_string_literals(sql or "")
    text = " ".join(cased.replace("if not exists", " ").split())
    return text.rstrip("; ").strip()


# The exact declared v5 shape: id primary key, required NOT NULL/typed columns,
# UNIQUE on endpoint_id and superseded_by, the two default-action foreign keys, the
# lifecycle enum CHECK, and the lifecycle/superseded_by relationship CHECK.
_CANONICAL_ENDPOINT_PROFILES_DDL = _canonical_table_ddl(ENDPOINT_PROFILES_DDL)

# The exact declared v6 (SEC-P30) shapes. Each fingerprint pins the whole table
# body -- primary key, NOT NULL/types/defaults, every UNIQUE and foreign key with
# its exact target, and every enum/relationship/state CHECK with quoted literal
# case preserved -- so a stamped-v6 table that weakens any single rail no longer
# fingerprints as current. The partial one-open index lives outside the table body
# (a separate CREATE INDEX), so its exact predicate is fingerprinted separately.
_CANONICAL_PROFILE_DECISIONS_DDL = _canonical_table_ddl(PROFILE_DECISIONS_DDL)
_CANONICAL_REPAIR_TASKS_DDL = _canonical_table_ddl(REPAIR_TASKS_DDL)
_CANONICAL_REPAIR_EVENTS_DDL = _canonical_table_ddl(REPAIR_EVENTS_DDL)
_CANONICAL_REPAIR_OPEN_INDEX_DDL = _canonical_table_ddl(REPAIR_TASKS_OPEN_INDEX_DDL)


def backfill_active_profiles(con: sqlite3.Connection, tenant_id: str, now: str) -> int:
    """Insert one active profile for each settlement endpoint that lacks one.

    Additive and idempotent: an endpoint that already carries a profile is
    skipped, so a fresh seed and a migrated database converge on the same
    registry state. Returns the number of profiles created.
    """
    created = 0
    for (endpoint_id,) in con.execute("SELECT id FROM settlement_endpoints ORDER BY id").fetchall():
        already = con.execute(
            "SELECT 1 FROM endpoint_profiles WHERE endpoint_id = ? LIMIT 1", (endpoint_id,)
        ).fetchone()
        if already:
            continue
        con.execute(
            "INSERT INTO endpoint_profiles(tenant_id, endpoint_id, lifecycle_state, superseded_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (tenant_id, endpoint_id, "active", None, now, now),
        )
        created += 1
    return created


class MigrationError(Exception):
    """A stable, deterministic migration failure. Raised fail-closed.

    ``code`` is a stable machine-readable slug for tests and callers; ``message``
    is the human-readable detail.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_seed_meta_version(con: sqlite3.Connection):
    """Return seed_meta.schema_version as an int, or None if absent."""
    try:
        row = con.execute(
            "SELECT value FROM seed_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # seed_meta table itself does not exist
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        raise MigrationError(
            "unknown_version",
            f"seed_meta.schema_version is not an integer: {row[0]!r}",
        )


def detect_version(con: sqlite3.Connection) -> int:
    """Resolve the current schema version of an existing DB, failing closed.

    Rules:
      * ``user_version == 0``: undetermined unless it is the known legacy state
        (``seed_meta.schema_version == 3``); anything else refuses to guess.
      * ``user_version >= 1``: authoritative, and must agree with a present
        ``seed_meta.schema_version``; a missing or disagreeing mirror is
        inconsistent.
    """
    user_version = con.execute("PRAGMA user_version").fetchone()[0]
    seed_meta_version = _read_seed_meta_version(con)

    if user_version == 0:
        if seed_meta_version == BASELINE_SCHEMA_VERSION:
            return BASELINE_SCHEMA_VERSION
        raise MigrationError(
            "unknown_version",
            "cannot determine schema version: PRAGMA user_version=0 with "
            f"seed_meta.schema_version={seed_meta_version!r}; refusing to guess",
        )

    if seed_meta_version is None:
        raise MigrationError(
            "inconsistent_version",
            f"PRAGMA user_version={user_version} is set but "
            "seed_meta.schema_version is absent",
        )
    if seed_meta_version != user_version:
        raise MigrationError(
            "inconsistent_version",
            f"PRAGMA user_version={user_version} disagrees with "
            f"seed_meta.schema_version={seed_meta_version}",
        )
    return user_version


def _migrate_3_to_4(con: sqlite3.Connection) -> None:
    """Foundation step 3 -> 4: introduce the schema_migrations ledger only.

    Metadata only. No business table is created, altered, or dropped, and no row
    in operator_actions or audit_events is read or written.

    Uses execute() rather than executescript(): the runner drives an explicit
    transaction, and executescript() would commit it early. Migration steps must
    therefore issue single statements so their effects roll back atomically.
    """
    con.execute(SCHEMA_MIGRATIONS_DDL)


def _migrate_4_to_5(con: sqlite3.Connection) -> None:
    """Profile-registry step 4 -> 5: add endpoint_profiles and backfill.

    Additive only: it creates the endpoint_profiles table and inserts one active
    profile per existing settlement endpoint. No existing business row is read
    for mutation, altered, or dropped, so operator actions, audit events, and
    every constituent record are preserved byte-for-byte.

    Each statement is issued via execute() (not executescript()) so the runner's
    single transaction wraps the table creation and every backfill insert; a
    failure rolls the whole step back atomically.
    """
    con.execute(ENDPOINT_PROFILES_DDL)
    backfill_active_profiles(con, SYNTHETIC_TENANT_ID, _now())


def _migrate_5_to_6(con: sqlite3.Connection) -> None:
    """Repair/decision step 5 -> 6: add profile_decisions, repair_tasks, repair_events.

    Additive only: it creates the three SEC-P30 tables (and the repair partial
    UNIQUE index) as empty tables. No existing business row -- profile, endpoint,
    operator action, or audit event -- is read for mutation, altered, or dropped,
    so every prior record is preserved byte-for-byte.

    Each statement is issued via execute() (not executescript()) so the runner's
    single transaction wraps all of it; a failure rolls the whole step back
    atomically.
    """
    con.execute(PROFILE_DECISIONS_DDL)
    con.execute(REPAIR_TASKS_DDL)
    con.execute(REPAIR_TASKS_OPEN_INDEX_DDL)
    con.execute(REPAIR_EVENTS_DDL)


# Explicit, ordered, versioned registry. Each entry is
# (from_version, to_version, name, apply_fn). Add future steps in order.
MIGRATIONS = [
    (BASELINE_SCHEMA_VERSION, 4, FOUNDATION_MIGRATION_NAME, _migrate_3_to_4),
    (4, 5, PROFILES_MIGRATION_NAME, _migrate_4_to_5),
    (5, CURRENT_SCHEMA_VERSION, REPAIR_DECISIONS_MIGRATION_NAME, _migrate_5_to_6),
]


def current_ledger():
    """The ledger rows (version, name) a DB at CURRENT_SCHEMA_VERSION should hold.

    Derived from the registry so a freshly seeded DB and a fully migrated DB
    converge on identical ledger content.
    """
    return [(to_v, name) for (_from_v, to_v, name, _fn) in MIGRATIONS]


def _read_current_ledger(con: sqlite3.Connection):
    """Return the (version, name) ledger rows, or None if the table is absent or
    the wrong shape."""
    try:
        rows = con.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return [tuple(row) for row in rows]


def _validate_ledger_prefix(con: sqlite3.Connection, version: int) -> None:
    """Fail closed unless the ledger equals the expected prefix through ``version``.

    Version markers alone can be forged or left stale, so a database at a given
    version must also carry exactly the foundation-ledger rows up to that
    version -- no stray, missing, or out-of-order rows. Validating the *prefix*
    at every step (not only the CURRENT-reaching step) keeps a multi-step chain
    fail-closed: a foreign or stale ledger row is caught at the first step and
    rolled back, rather than surviving an intermediate commit. Only the
    (version, name) rows are compared; the applied_at timestamp is free-form.
    """
    ledger = _read_current_ledger(con)
    expected = [(v, n) for (v, n) in current_ledger() if v <= version]
    if ledger is None:
        raise MigrationError(
            "inconsistent_version",
            "version markers advanced but the schema_migrations ledger is "
            "missing or malformed",
        )
    if ledger != expected:
        raise MigrationError(
            "inconsistent_version",
            f"schema_migrations ledger {ledger} does not match the expected "
            f"foundation prefix {expected} through version {version}",
        )


def _validate_current_ledger(con: sqlite3.Connection) -> None:
    """Fail closed unless the ledger exactly matches the expected CURRENT state."""
    _validate_ledger_prefix(con, CURRENT_SCHEMA_VERSION)


def _validate_repair_schema(con: sqlite3.Connection) -> None:
    """Fail closed unless the v6 SEC-P30 tables match their exact declared shape.

    The versioned decision ledger, the repair workflow table, and the append-only
    repair-event trail must each fingerprint identically to their declared DDL, and
    the one-open partial UNIQUE index must carry its exact predicate. The canonical
    fingerprint already pins PK / types / NOT NULL / defaults, every UNIQUE and
    foreign key with its exact target, and every enum / relationship / state CHECK
    with quoted-literal case preserved, so a stamped-v6 table that weakens any
    single rail no longer fingerprints as current. Compares only the declared DDL,
    never the rows.
    """
    for name, canonical in (
        ("profile_decisions", _CANONICAL_PROFILE_DECISIONS_DDL),
        ("repair_tasks", _CANONICAL_REPAIR_TASKS_DDL),
        ("repair_events", _CANONICAL_REPAIR_EVENTS_DDL),
    ):
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if row is None or not row[0]:
            raise MigrationError(
                "inconsistent_version",
                f"current schema claims v{CURRENT_SCHEMA_VERSION} but the {name} table is missing",
            )
        if _canonical_table_ddl(row[0]) != canonical:
            raise MigrationError(
                "inconsistent_version",
                f"{name} does not match the exact declared v6 schema",
            )

    # The one-open partial UNIQUE index lives outside the table body (a separate
    # CREATE INDEX), so its exact predicate is fingerprinted on its own: a missing,
    # full (non-partial), or wrong-predicate index no longer enforces at most one
    # live repair task per profile.
    index_row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='repair_tasks_one_open_per_profile'"
    ).fetchone()
    if index_row is None or not index_row[0]:
        raise MigrationError(
            "inconsistent_version",
            "repair_tasks is missing the one-open-per-profile partial UNIQUE index",
        )
    if _canonical_table_ddl(index_row[0]) != _CANONICAL_REPAIR_OPEN_INDEX_DDL:
        raise MigrationError(
            "inconsistent_version",
            "the repair_tasks one-open-per-profile index does not match its exact declared predicate",
        )


def _validate_repair_row_integrity(con: sqlite3.Connection) -> None:
    """Fail closed unless the current v6 decision/repair/event rows obey the
    cross-row invariants no per-row SQLite constraint can prove.

    ``_validate_repair_schema`` pins the declared rails; this proves the DATA
    honours them. Every row is scoped to the server-owned tenant and carries a
    known evidence/event enum (revalidation evidence is enum-restricted; a baseline
    snapshots intrinsic constituent values verbatim); each profile's decisions form
    exactly a contiguous 1..N baseline-then-revalidation chain (each revalidation
    linking to the immediately prior same-profile/same-tenant decision, which --
    with contiguous versions -- forbids branches, cycles, and dangling/foreign
    links); each stored decision's full output equals the deterministic evaluator
    output over the inputs it represents (a baseline over the profile's intrinsic
    constituent fields, a revalidation over its refreshed evidence snapshot); each
    repair task's opened/resolved decisions, state/field presence, and monotonic
    step timestamps cohere with that chain, a resolved task's refreshed evidence and
    resolution time equal its resolving decision's snapshot and timestamp, a live
    task sits only on an active profile, and at most one task is live per profile;
    every decision is in turn owned by the workflow (a v1 baseline opened by exactly
    one task, a v>1 revalidation resolved by exactly one task, each decision opened
    by at most one task), so an orphan evaluator-consistent chain with no owning
    task/event cannot pass; and each task's event trail is exactly the legal prefix
    for its state, carrying
    the task's actor, the declared per-step decision linkage, and step-aligned
    timestamps. Empty tables (a fresh v6 seed) satisfy every invariant. Bounded
    reads only; never mutates.
    """
    # (a) Tenant scoping. The running app reads only the single server-owned tenant,
    # so a foreign or null-tenant row would be silently invisible to it.
    for table in ("profile_decisions", "repair_tasks", "repair_events"):
        foreign = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id IS NULL OR tenant_id <> ?",
            (SYNTHETIC_TENANT_ID,),
        ).fetchone()[0]
        if foreign:
            raise MigrationError(
                "inconsistent_version",
                f"a {table} row is scoped to a tenant other than the server-owned tenant",
            )

    # (b) Evidence/event/actor enum domains. The DDL constrains evidence presence
    # per state but not the value set, so a stored status or actor outside the API
    # domain -- or an unknown event kind -- is a corrupt row a per-row CHECK cannot
    # catch.
    def _reject_unknown_enum(table, column, allowed, *, nullable=False, extra=""):
        members = ",".join("?" for _ in allowed)
        if nullable:
            clause = f"{column} IS NOT NULL AND {column} NOT IN ({members})"
        else:
            clause = f"{column} IS NULL OR {column} NOT IN ({members})"
        if extra:
            clause = f"({clause}) AND {extra}"
        if con.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", tuple(allowed)).fetchone()[0]:
            raise MigrationError("inconsistent_version", f"a {table} row has an unsupported {column} value")

    def _parse_audit_timestamp(value, label):
        """Return a parsed canonical second-resolution UTC timestamp or fail closed."""
        if not isinstance(value, str):
            raise MigrationError("inconsistent_version", f"{label} is not a canonical UTC timestamp")
        try:
            parsed = datetime.strptime(value, AUDIT_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise MigrationError(
                "inconsistent_version", f"{label} is not a canonical UTC timestamp"
            ) from exc
        if parsed.strftime(AUDIT_TIMESTAMP_FORMAT) != value:
            raise MigrationError("inconsistent_version", f"{label} is not a canonical UTC timestamp")
        return parsed

    # Refreshed (revalidation-origin) decision evidence is restricted to the exact
    # evaluator-supported enum sets. A baseline decision instead snapshots the
    # profile's intrinsic constituent fields verbatim, which for a legacy/backfilled
    # fixture may be a scenario_driven placeholder; its evidence is validated by
    # exact equality to those intrinsic fields in the deterministic-decision check
    # below, so it is deliberately not enum-restricted here.
    _reject_unknown_enum("profile_decisions", "evidence_authority_status", REFRESHED_AUTHORITY_STATES, extra="origin = 'revalidation'")
    _reject_unknown_enum("profile_decisions", "evidence_allowlist_status", REFRESHED_ALLOWLIST_STATES, extra="origin = 'revalidation'")
    _reject_unknown_enum("profile_decisions", "evidence_payload_status", REFRESHED_PAYLOAD_STATES, extra="origin = 'revalidation'")
    _reject_unknown_enum("repair_tasks", "refreshed_authority_status", REFRESHED_AUTHORITY_STATES, nullable=True)
    _reject_unknown_enum("repair_tasks", "refreshed_allowlist_status", REFRESHED_ALLOWLIST_STATES, nullable=True)
    _reject_unknown_enum("repair_tasks", "refreshed_payload_status", REFRESHED_PAYLOAD_STATES, nullable=True)
    _reject_unknown_enum("repair_tasks", "actor", REPAIR_ACTORS)
    _reject_unknown_enum("repair_events", "event_type", ("repair_opened", "evidence_refreshed", "revalidated"))
    _reject_unknown_enum("repair_events", "actor", REPAIR_ACTORS)

    # Load the three (small, synthetic) tables once for the relational checks. The
    # full decision output and evidence snapshot are loaded too, so the stored
    # decision can be checked against the deterministic evaluator output.
    decision_by_id = {
        r[0]: {
            "id": r[0], "tenant_id": r[1], "profile_id": r[2], "version": r[3],
            "previous_decision_id": r[4], "origin": r[5],
            "decision": {
                "verdict": r[6], "token_class": r[7], "fiat_class": r[8],
                "token_text": r[9], "fiat_text": r[10], "repair_text": r[11],
            },
            "evidence": {"authority_status": r[12], "allowlist_status": r[13], "payload_status": r[14]},
            "created_at": r[15],
            "created_at_dt": _parse_audit_timestamp(r[15], "a profile_decisions.created_at value"),
        }
        for r in con.execute(
            "SELECT id, tenant_id, profile_id, version, previous_decision_id, origin,"
            " verdict, token_class, fiat_class, token_text, fiat_text, repair_text,"
            " evidence_authority_status, evidence_allowlist_status, evidence_payload_status, created_at"
            " FROM profile_decisions"
        ).fetchall()
    }
    tasks = con.execute(
        "SELECT id, tenant_id, profile_id, actor, state, opened_decision_id, resolved_decision_id,"
        " refreshed_authority_status, refreshed_allowlist_status, refreshed_payload_status,"
        " created_at, evidence_refreshed_at, resolved_at FROM repair_tasks"
    ).fetchall()
    events = con.execute(
        "SELECT task_id, profile_id, tenant_id, sequence, event_type, actor, decision_id, created_at"
        " FROM repair_events"
    ).fetchall()

    # (b2) Profile resolution. Every decision and repair task must reference an
    # endpoint_profiles row that exists under the SAME server-owned tenant. The
    # declared profile_id foreign key is not enforced retroactively on already
    # stored rows, so a self-consistent chain or task can still name a profile_id
    # absent from endpoint_profiles (or present only under another tenant) -- a
    # row the running app, which reads only its own tenant's profiles, would never
    # surface. Resolve each referenced (profile_id, tenant_id) pair once.
    profile_rows = con.execute("SELECT id, tenant_id, lifecycle_state FROM endpoint_profiles").fetchall()
    profile_tenant = {row[0]: row[1] for row in profile_rows}
    profile_state = {row[0]: row[2] for row in profile_rows}
    referenced = (
        [("profile_decisions", d["profile_id"], d["tenant_id"]) for d in decision_by_id.values()]
        + [("repair_tasks", task[2], task[1]) for task in tasks]  # tasks: (id, tenant_id, profile_id, ...)
    )
    for table, profile_id, tenant in referenced:
        if profile_tenant.get(profile_id) != tenant:
            raise MigrationError(
                "inconsistent_version",
                f"a {table} row references a profile_id absent from endpoint_profiles under its tenant",
            )

    # (c) Decision chains: exactly a contiguous 1..N baseline-then-revalidation
    # lineage per profile. previous_decision_id is UNIQUE (schema), so no two
    # decisions share a predecessor; here each link must resolve to the immediately
    # prior same-profile/same-tenant decision -- with contiguous versions this
    # forbids any branch, cycle, or dangling/foreign link.
    by_profile = {}
    for decision in decision_by_id.values():
        by_profile.setdefault(decision["profile_id"], []).append(decision)
    for chain in by_profile.values():
        chain.sort(key=lambda d: d["version"])
        if [d["version"] for d in chain] != list(range(1, len(chain) + 1)):
            raise MigrationError("inconsistent_version", "a profile's decision versions are not contiguous 1..N")
        for decision in chain:
            if decision["version"] == 1:
                if decision["origin"] != "baseline" or decision["previous_decision_id"] is not None:
                    raise MigrationError("inconsistent_version", "a v1 decision is not a predecessor-free baseline")
                continue
            predecessor = decision_by_id.get(decision["previous_decision_id"])
            if (decision["origin"] != "revalidation" or predecessor is None
                    or predecessor["profile_id"] != decision["profile_id"]
                    or predecessor["tenant_id"] != decision["tenant_id"]
                    or predecessor["version"] != decision["version"] - 1):
                raise MigrationError(
                    "inconsistent_version",
                    "a decision does not link to the immediately prior same-profile decision",
                )

    # (c2) Deterministic decision snapshot. A stored decision is only trustworthy if
    # its full persisted output equals the evaluator's output over the exact inputs
    # the decision represents. Validation is origin-aware:
    #   * a baseline decision's evidence must equal the profile's INTRINSIC graded
    #     constituent fields (authority from the legal entity, allowlist/payload from
    #     the settlement endpoint) verbatim -- even a legacy scenario_driven value --
    #     and its decision must equal evaluate() over those intrinsic inputs plus the
    #     profile's immutable fallback/structure;
    #   * a revalidation decision's decision must equal evaluate() over the profile's
    #     immutable structure/fallback plus its own refreshed evidence snapshot
    #     (already enum-restricted above).
    # Uses the real evaluator via a safe local import (evaluator imports neither app
    # nor migrate, so there is no import cycle) and a narrow local input mapper.
    from evaluator import evaluate, APPROVED  # local import: no app<->migrate cycle

    constituents_cache = {}

    def _profile_constituents(profile_id):
        # (intrinsic_authority, intrinsic_allowlist, intrinsic_payload, rail,
        # currency, mask, bic) for a profile's immutable constituent rows.
        if profile_id not in constituents_cache:
            row = con.execute(
                "SELECT le.authority_status, se.allowlist_status, se.endpoint_payload_status,"
                " se.fallback_rail, se.fallback_currency, se.fallback_account_mask, se.fallback_intermediary_bic"
                " FROM endpoint_profiles ep"
                " JOIN settlement_endpoints se ON se.id = ep.endpoint_id"
                " JOIN legal_entities le ON le.id = se.legal_entity_id"
                " WHERE ep.id = ?",
                (profile_id,),
            ).fetchone()
            if row is None:
                raise MigrationError(
                    "inconsistent_version",
                    "a profile decision references missing endpoint or legal-entity constituent data",
                )
            constituents_cache[profile_id] = row
        return constituents_cache[profile_id]

    def _evaluator_input(constituents, authority, allowlist, payload):
        return {
            "institution_present": True,
            "institution_reachable": True,
            "legal_entity_present": True,
            "authority_status": authority,
            "allowlist_status": allowlist,
            "payload_status": payload,
            "fallback_rail": constituents[3],
            "fallback_currency": constituents[4],
            "fallback_account_mask": constituents[5],
            "fallback_intermediary_bic": constituents[6],
        }

    for decision in decision_by_id.values():
        constituents = _profile_constituents(decision["profile_id"])
        evidence = decision["evidence"]
        if decision["origin"] == "baseline":
            intrinsic = (constituents[0], constituents[1], constituents[2])
            if (evidence["authority_status"], evidence["allowlist_status"], evidence["payload_status"]) != intrinsic:
                raise MigrationError(
                    "inconsistent_version",
                    "a baseline decision's evidence does not equal the profile's intrinsic constituent fields",
                )
            authority, allowlist, payload = intrinsic
        else:
            authority = evidence["authority_status"]
            allowlist = evidence["allowlist_status"]
            payload = evidence["payload_status"]
        expected = evaluate(_evaluator_input(constituents, authority, allowlist, payload))["decision"]
        if decision["decision"] != expected:
            raise MigrationError(
                "inconsistent_version",
                "a stored decision is not consistent with the deterministic evaluator output",
            )

    # (d) Repair tasks: the opened (and, once resolved, the resolving) decision must
    # be same-profile/same-tenant and sit adjacently in that profile's chain; the
    # state must cohere with evidence/resolution presence and monotonic timestamps;
    # a live (non-resolved) task may only sit on an ACTIVE profile; and at most one
    # task may be live per profile.
    task_by_id = {}
    live_per_profile = {}
    for (tid, tenant, profile_id, actor, state, opened_id, resolved_id,
         auth, allow, payload, created_at, refreshed_at, resolved_at) in tasks:
        created_at_dt = _parse_audit_timestamp(created_at, "a repair_tasks.created_at value")
        refreshed_at_dt = (
            _parse_audit_timestamp(refreshed_at, "a repair_tasks.evidence_refreshed_at value")
            if refreshed_at is not None else None
        )
        resolved_at_dt = (
            _parse_audit_timestamp(resolved_at, "a repair_tasks.resolved_at value")
            if resolved_at is not None else None
        )
        task_by_id[tid] = {
            "id": tid, "profile_id": profile_id, "tenant_id": tenant, "actor": actor, "state": state,
            "opened_decision_id": opened_id, "resolved_decision_id": resolved_id,
            "created_at": created_at, "evidence_refreshed_at": refreshed_at, "resolved_at": resolved_at,
            "created_at_dt": created_at_dt, "evidence_refreshed_at_dt": refreshed_at_dt,
            "resolved_at_dt": resolved_at_dt,
        }
        opened = decision_by_id.get(opened_id)
        if opened is None or opened["profile_id"] != profile_id or opened["tenant_id"] != tenant:
            raise MigrationError("inconsistent_version", "a repair task's opened decision is missing or mis-scoped")
        # Hold-only provenance. A repair only ever opens a FAILING decision: the app
        # returns nothing_to_repair when the latest decision is APPROVED, so a task
        # whose opened decision reads as APPROVED has no basis to repair and cannot
        # be a real workflow row (every legal opened decision is a BLOCKED/HOLD).
        if opened["decision"]["verdict"] == APPROVED:
            raise MigrationError("inconsistent_version", "a repair task opens an already-approved decision")
        # Opened-decision chronology. A task cannot be recorded before the decision it
        # opens exists: the v1 baseline is created ATOMICALLY by the first open, so the
        # task shares that baseline's timestamp exactly; a later repair opens a
        # pre-existing (revalidation) decision and so is recorded at or after it. This
        # is cross-row and per-task timestamp monotonicity alone cannot see it.
        if created_at_dt < opened["created_at_dt"]:
            raise MigrationError("inconsistent_version", "a repair task is recorded before the decision it opens")
        if opened["version"] == 1 and created_at_dt != opened["created_at_dt"]:
            raise MigrationError("inconsistent_version", "a v1 baseline open is not atomic with its baseline decision")
        refreshed_set = auth is not None and allow is not None and payload is not None
        refreshed_clear = auth is None and allow is None and payload is None
        if state == "open":
            coherent = refreshed_clear and refreshed_at is None and resolved_id is None and resolved_at is None
            timeline = [created_at_dt]
        elif state == "evidence_refreshed":
            coherent = refreshed_set and refreshed_at is not None and resolved_id is None and resolved_at is None
            timeline = [created_at_dt, refreshed_at_dt]
        elif state == "resolved":
            coherent = refreshed_set and refreshed_at is not None and resolved_id is not None and resolved_at is not None
            timeline = [created_at_dt, refreshed_at_dt, resolved_at_dt]
        else:  # unreachable once the schema fingerprint holds, but stay fail-closed
            coherent, timeline = False, [created_at_dt]
        if not coherent:
            raise MigrationError(
                "inconsistent_version", "a repair task's state and evidence/resolution fields are incoherent"
            )
        if any(a is None or b is None or a > b for a, b in zip(timeline, timeline[1:])):
            raise MigrationError("inconsistent_version", "a repair task's step timestamps are not monotonic")
        if resolved_id is not None:
            resolved = decision_by_id.get(resolved_id)
            if (resolved is None or resolved["profile_id"] != profile_id or resolved["tenant_id"] != tenant
                    or resolved["origin"] != "revalidation" or resolved["previous_decision_id"] != opened_id
                    or resolved["version"] != opened["version"] + 1):
                raise MigrationError(
                    "inconsistent_version", "a resolved repair task does not supersede its opened decision"
                )
            # The resolving decision snapshots exactly the task's refreshed evidence,
            # and its timestamp is the task's resolution instant. Otherwise the
            # persisted advisory verdict would not be the one the refreshed evidence
            # actually produced at resolution time.
            resolved_evidence = resolved["evidence"]
            if (resolved_evidence["authority_status"], resolved_evidence["allowlist_status"],
                    resolved_evidence["payload_status"]) != (auth, allow, payload):
                raise MigrationError(
                    "inconsistent_version",
                    "a resolved task's refreshed evidence does not equal its resolving decision snapshot",
                )
            if resolved["created_at_dt"] != resolved_at_dt:
                raise MigrationError(
                    "inconsistent_version",
                    "a resolving decision's timestamp does not equal the task's resolved_at",
                )
        profile_lifecycle = profile_state.get(profile_id)
        # Repair history can only originate while a profile is ACTIVE. Resolved
        # history may remain queryable after supersession, but a DRAFT has never
        # been eligible to open a repair and therefore cannot legally carry any
        # task/decision/event chain, resolved or otherwise.
        if profile_lifecycle == "draft":
            raise MigrationError(
                "inconsistent_version", "a draft profile carries repair history"
            )
        if state != "resolved":
            # A live repair only makes sense on an ACTIVE profile: the workflow may
            # not leave or advance a repair on a superseded profile.
            if profile_lifecycle != "active":
                raise MigrationError(
                    "inconsistent_version", "a live repair task sits on a non-active profile"
                )
            live_per_profile[profile_id] = live_per_profile.get(profile_id, 0) + 1
            if live_per_profile[profile_id] > 1:
                raise MigrationError("inconsistent_version", "a profile has more than one live repair task")

    # (d2) Reverse decision provenance. The forward checks above prove every task
    # points at coherent decisions; the converse must also hold, or an
    # evaluator-consistent decision chain with NO owning task/event -- which reads
    # would still promote as current -- would pass with no action/event backing it.
    # Every decision is recorded by a repair step: a v1 baseline is what an open
    # records, so exactly one task opens it; a v>1 revalidation is what a resolve
    # records, so exactly one task resolves it (resolved_decision_id is UNIQUE, so
    # never more than one). A decision is opened by at most one task -- a later HOLD
    # is opened once by the next repair cycle, a terminal latest decision zero times
    # -- so the legal live-open/evidence, resolved, and repeated HOLD->repair
    # histories are preserved while an orphan chain is not. Once reverse ownership is
    # proved, the task-owned event-prefix check (e) supplies each decision's event
    # provenance transitively.
    opened_by_count = {}
    resolved_by_count = {}
    for task in task_by_id.values():
        opened_by_count[task["opened_decision_id"]] = opened_by_count.get(task["opened_decision_id"], 0) + 1
        if task["resolved_decision_id"] is not None:
            resolved_by_count[task["resolved_decision_id"]] = resolved_by_count.get(task["resolved_decision_id"], 0) + 1
    for decision in decision_by_id.values():
        opened = opened_by_count.get(decision["id"], 0)
        if opened > 1:
            raise MigrationError("inconsistent_version", "a decision is opened by more than one repair task")
        if decision["version"] == 1:
            if opened != 1:
                raise MigrationError(
                    "inconsistent_version", "a v1 baseline decision is not opened by exactly one repair task"
                )
        elif resolved_by_count.get(decision["id"], 0) != 1:
            raise MigrationError(
                "inconsistent_version", "a revalidation decision is not resolved by exactly one repair task"
            )

    # (e) Event trail: exactly the legal prefix for each task's state, task/profile/
    # tenant scoped, carrying the task's actor, the declared per-step decision
    # linkage (open -> opened decision, evidence -> none, revalidation -> resolving
    # decision), and step-aligned timestamps.
    events_by_task = {}
    for event in events:
        events_by_task.setdefault(event[0], []).append(event)
    if set(events_by_task) - set(task_by_id):
        raise MigrationError("inconsistent_version", "a repair event references an unknown task")
    legal_prefix = [(1, "repair_opened"), (2, "evidence_refreshed"), (3, "revalidated")]
    steps_for_state = {"open": 1, "evidence_refreshed": 2, "resolved": 3}
    for tid, task in task_by_id.items():
        trail = sorted(events_by_task.get(tid, []), key=lambda e: e[3])  # by sequence
        if [(e[3], e[4]) for e in trail] != legal_prefix[: steps_for_state[task["state"]]]:
            raise MigrationError(
                "inconsistent_version", "a repair task's event trail is not the legal prefix for its state"
            )
        decision_for_step = {1: task["opened_decision_id"], 2: None, 3: task["resolved_decision_id"]}
        time_for_step = {
            1: task["created_at_dt"], 2: task["evidence_refreshed_at_dt"], 3: task["resolved_at_dt"],
        }
        for (etask, eprofile, etenant, seq, etype, eactor, edecision, ecreated) in trail:
            event_time = _parse_audit_timestamp(ecreated, "a repair_events.created_at value")
            if eprofile != task["profile_id"] or etenant != task["tenant_id"] or eactor != task["actor"]:
                raise MigrationError("inconsistent_version", "a repair event is mis-scoped or has the wrong actor")
            if edecision != decision_for_step[seq]:
                raise MigrationError("inconsistent_version", "a repair event references the wrong decision")
            if event_time != time_for_step[seq]:
                raise MigrationError("inconsistent_version", "a repair event timestamp does not match its task step")


def _validate_current_schema(con: sqlite3.Connection) -> None:
    """Fail closed unless the current endpoint_profiles registry is well-formed.

    Verifies only the named current-schema invariants -- not a general schema
    introspector: the table exists with the required columns and the
    settlement_endpoints foreign key, every settlement endpoint is covered by
    exactly one profile, every profile is scoped to the server-owned tenant, and
    every lifecycle value and superseded_by relationship is valid. Any sqlite
    error raised while validating (e.g. an absent or unreadable
    settlement_endpoints table) is converted into the same stable
    ``inconsistent_version`` failure so the command never tracebacks; the caller
    preserves state.
    """
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(endpoint_profiles)").fetchall()]
        if not columns:
            raise MigrationError(
                "inconsistent_version",
                f"current schema claims v{CURRENT_SCHEMA_VERSION} but the endpoint_profiles table is missing",
            )
        missing = [c for c in REQUIRED_PROFILE_COLUMNS if c not in columns]
        if missing:
            raise MigrationError("inconsistent_version", f"endpoint_profiles is missing required column(s) {missing}")

        # Foreign key endpoint_id -> settlement_endpoints(id) must be present with
        # the EXACT target column (id), not merely the right source table/column.
        # PRAGMA foreign_key_list rows are (id, seq, table, from, to, on_update, on_delete, match).
        foreign_keys = con.execute("PRAGMA foreign_key_list(endpoint_profiles)").fetchall()
        if not any(fk[2] == "settlement_endpoints" and fk[3] == "endpoint_id" and fk[4] == "id" for fk in foreign_keys):
            raise MigrationError(
                "inconsistent_version",
                "endpoint_profiles is missing the exact endpoint_id -> settlement_endpoints(id) foreign key",
            )

        # Every settlement endpoint is covered by exactly one profile (a bijection):
        # none uncovered, none covered twice, and no profile pointing at a missing endpoint.
        uncovered = con.execute(
            "SELECT COUNT(*) FROM settlement_endpoints se "
            "WHERE NOT EXISTS (SELECT 1 FROM endpoint_profiles ep WHERE ep.endpoint_id = se.id)"
        ).fetchone()[0]
        if uncovered:
            raise MigrationError("inconsistent_version", f"{uncovered} settlement endpoint(s) have no endpoint_profile")
        duplicated = con.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM endpoint_profiles GROUP BY endpoint_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if duplicated:
            raise MigrationError("inconsistent_version", "a settlement endpoint is covered by more than one endpoint_profile")
        dangling_endpoint = con.execute(
            "SELECT COUNT(*) FROM endpoint_profiles ep "
            "WHERE NOT EXISTS (SELECT 1 FROM settlement_endpoints se WHERE se.id = ep.endpoint_id)"
        ).fetchone()[0]
        if dangling_endpoint:
            raise MigrationError("inconsistent_version", "an endpoint_profile references a missing settlement endpoint")

        # Every profile must be scoped to the single server-owned tenant. The app
        # reads only that tenant, so a foreign-tenant (or null) row would report
        # already_current yet be invisible to the running service.
        wrong_tenant = con.execute(
            "SELECT COUNT(*) FROM endpoint_profiles WHERE tenant_id <> ? OR tenant_id IS NULL",
            (SYNTHETIC_TENANT_ID,),
        ).fetchone()[0]
        if wrong_tenant:
            raise MigrationError(
                "inconsistent_version",
                "an endpoint_profile is scoped to a tenant other than the server-owned tenant",
            )

        # Lifecycle values and superseded_by relationships must be valid.
        bad_state = con.execute(
            "SELECT COUNT(*) FROM endpoint_profiles WHERE lifecycle_state IS NULL "
            "OR lifecycle_state NOT IN ('draft','active','superseded')"
        ).fetchone()[0]
        if bad_state:
            raise MigrationError("inconsistent_version", "an endpoint_profile has an unknown lifecycle_state")
        bad_link = con.execute(
            "SELECT COUNT(*) FROM endpoint_profiles WHERE "
            "(lifecycle_state IN ('draft','active') AND superseded_by IS NOT NULL) "
            "OR (lifecycle_state = 'superseded' AND (superseded_by IS NULL OR superseded_by = id))"
        ).fetchone()[0]
        if bad_link:
            raise MigrationError(
                "inconsistent_version",
                "an endpoint_profile has an invalid lifecycle/superseded_by relationship",
            )
        dangling_link = con.execute(
            "SELECT COUNT(*) FROM endpoint_profiles ep WHERE ep.superseded_by IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM endpoint_profiles r WHERE r.id = ep.superseded_by)"
        ).fetchone()[0]
        if dangling_link:
            raise MigrationError("inconsistent_version", "an endpoint_profile superseded_by references a missing profile")

        # A supersession target must be a live replacement, never a draft: the
        # state machine activates a replacement atomically, so a superseded ->
        # draft link is a corrupt history the per-row CHECK cannot see (it cannot
        # inspect the target's lifecycle_state).
        draft_target = con.execute(
            "SELECT COUNT(*) FROM endpoint_profiles ep "
            "JOIN endpoint_profiles target ON target.id = ep.superseded_by "
            "WHERE ep.superseded_by IS NOT NULL AND target.lifecycle_state = 'draft'"
        ).fetchone()[0]
        if draft_target:
            raise MigrationError("inconsistent_version", "an endpoint_profile is superseded by a draft replacement")

        # Replacement history is one-to-one: a replacement supersedes at most one
        # profile, so two profiles sharing a superseded_by target is reuse/branching.
        branched = con.execute(
            "SELECT COUNT(*) FROM (SELECT superseded_by FROM endpoint_profiles "
            "WHERE superseded_by IS NOT NULL GROUP BY superseded_by HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if branched:
            raise MigrationError("inconsistent_version", "an endpoint_profile replacement is reused by more than one profile")

        # The superseded_by graph must be acyclic (every chain ends at an active
        # head). Each link resolves to an existing profile (dangling checked above),
        # so a bounded walk from each node detects any cycle -- including multi-hop
        # cycles a per-row self-link CHECK cannot see. Nodes proven to reach a
        # terminal are memoized so the whole sweep stays linear.
        successor = {
            row[0]: row[1]
            for row in con.execute("SELECT id, superseded_by FROM endpoint_profiles").fetchall()
        }
        acyclic = set()
        for start in successor:
            walked = set()
            node = start
            while node is not None and node not in acyclic:
                if node in walked:
                    raise MigrationError("inconsistent_version", "an endpoint_profile supersession chain contains a cycle")
                walked.add(node)
                node = successor.get(node)
            acyclic |= walked

        # Declared constraint rails. Current rows can satisfy every data check above
        # while the table has silently dropped a declared rail that guards future
        # writes. Validate the named NOT NULL / PK / UNIQUE / FK / CHECK rails with
        # bounded PRAGMA/sqlite_master reads (not a general schema framework); the
        # endpoint_id -> settlement_endpoints foreign key is already checked above.
        # PRAGMA table_info rows are (cid, name, type, notnull, dflt_value, pk).
        columns_meta = {row[1]: row for row in con.execute("PRAGMA table_info(endpoint_profiles)").fetchall()}
        if columns_meta["id"][5] < 1:
            raise MigrationError("inconsistent_version", "endpoint_profiles.id is not the declared primary key")
        for column in ("tenant_id", "endpoint_id", "lifecycle_state", "created_at", "updated_at"):
            if columns_meta[column][3] != 1:
                raise MigrationError("inconsistent_version", f"endpoint_profiles.{column} has dropped its NOT NULL constraint")

        # UNIQUE(endpoint_id): a full (non-partial) UNIQUE index over exactly that
        # column. PRAGMA index_list rows are (seq, name, unique, origin, partial);
        # index_info rows are (seqno, cid, name).
        def _has_unique_index_over(column: str) -> bool:
            for index in con.execute("PRAGMA index_list(endpoint_profiles)").fetchall():
                if index[2] != 1 or index[4] != 0:  # unique, non-partial
                    continue
                index_columns = [info[2] for info in con.execute(f'PRAGMA index_info("{index[1]}")').fetchall()]
                if index_columns == [column]:
                    return True
            return False

        if not _has_unique_index_over("endpoint_id"):
            raise MigrationError("inconsistent_version", "endpoint_profiles has dropped the UNIQUE(endpoint_id) constraint")
        # UNIQUE(superseded_by) keeps replacement history one-to-one; the DDL now
        # declares it, so a stamped-v5 table lacking it is not actually current.
        if not _has_unique_index_over("superseded_by"):
            raise MigrationError("inconsistent_version", "endpoint_profiles has dropped the UNIQUE(superseded_by) constraint")

        # Self-referential superseded_by -> endpoint_profiles(id) foreign key, with
        # the EXACT target column (id), not merely the right source table/column.
        self_fk = con.execute("PRAGMA foreign_key_list(endpoint_profiles)").fetchall()
        if not any(fk[2] == "endpoint_profiles" and fk[3] == "superseded_by" and fk[4] == "id" for fk in self_fk):
            raise MigrationError(
                "inconsistent_version",
                "endpoint_profiles is missing the exact superseded_by -> endpoint_profiles(id) self-foreign-key",
            )

        # lifecycle_state CHECK. PRAGMA cannot expose CHECK constraints, so inspect
        # the declared DDL text with whitespace removed (robust to reformatting):
        # the exact allowed set appears only in the column CHECK.
        table_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='endpoint_profiles'"
        ).fetchone()
        compact_ddl = "".join((table_sql[0] if table_sql and table_sql[0] else "").split()).lower()
        if "check(" not in compact_ddl or "lifecycle_statein('draft','active','superseded')" not in compact_ddl:
            raise MigrationError("inconsistent_version", "endpoint_profiles has dropped the lifecycle_state CHECK constraint")

        # Exact declared shape. The rails above catch the common single-drop cases
        # with specific messages; this canonical-DDL fingerprint is the exact
        # backstop -- it rejects any remaining structural drift that valid current
        # rows would hide: a composite/extra primary key, destructive FK actions
        # (non-default on_update/on_delete/match), extra or malformed foreign keys,
        # unexpected columns, and a removed or weakened lifecycle/superseded_by
        # relationship CHECK. It compares only the declared table DDL, never the
        # rows, so valid multi-hop current-v5 history is preserved.
        if _canonical_table_ddl(table_sql[0] if table_sql and table_sql[0] else "") != _CANONICAL_ENDPOINT_PROFILES_DDL:
            raise MigrationError(
                "inconsistent_version",
                "endpoint_profiles does not match the exact declared v5 schema",
            )

        # SEC-P30 (v6): the versioned decision ledger, the repair workflow table,
        # and the append-only repair-event trail must each match their exact
        # declared shape, the one-open partial UNIQUE index must carry its exact
        # predicate, and the current rows must satisfy the cross-row invariants
        # SQLite per-row constraints cannot prove.
        _validate_repair_schema(con)
        _validate_repair_row_integrity(con)
    except MigrationError:
        # An invariant violation is already a stable, deterministic failure; keep
        # its specific code/message rather than reclassifying (never swallow it).
        raise
    except sqlite3.Error as exc:
        # A raw sqlite error while validating (e.g. an absent/unreadable
        # settlement_endpoints table) becomes the same stable fail-closed result.
        raise MigrationError(
            "inconsistent_version",
            "the endpoint_profiles registry could not be validated at the current "
            f"schema version: {type(exc).__name__}",
        ) from exc


def _plan(migrations, start: int):
    """Return the contiguous list of steps advancing ``start`` to CURRENT.

    Fails closed on a gap (no step continues from the current pointer) or a plan
    that does not reach CURRENT_SCHEMA_VERSION.
    """
    plan = []
    version = start
    for from_v, to_v, name, fn in sorted(migrations, key=lambda step: step[0]):
        if from_v < start:
            continue
        if from_v != version:
            raise MigrationError(
                "unknown_version",
                f"no contiguous migration path: expected a step from {version} "
                f"but the next available starts at {from_v}",
            )
        plan.append((from_v, to_v, name, fn))
        version = to_v
    if version != CURRENT_SCHEMA_VERSION:
        # Any end-version other than CURRENT fails closed, including an empty plan
        # when start < CURRENT (e.g. an accidentally empty registry). Callers only
        # invoke this with start < CURRENT, so an empty plan is always a gap.
        raise MigrationError(
            "unknown_version",
            f"migration path ends at {version}, not current {CURRENT_SCHEMA_VERSION}",
        )
    return plan


def _record_migration(con: sqlite3.Connection, version: int, name: str, applied_at: str) -> None:
    con.execute(
        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
        (version, name, applied_at),
    )


def migrate(db_path=DB_PATH) -> dict:
    """Apply ordered forward migrations to a persistent local database in place.

    Always uses the explicit module registry (:data:`MIGRATIONS`). Returns a
    summary dict ``{status, from, to, applied}``. Raises ``MigrationError`` on any
    fail-closed condition or a rolled-back step.
    """
    path = Path(db_path)
    if not path.exists():
        raise MigrationError(
            "no_database",
            f"no SQLite database at {path}; run seed.py to create the synthetic fixture",
        )

    con = sqlite3.connect(path)
    con.isolation_level = None  # explicit BEGIN/COMMIT/ROLLBACK per step
    try:
        con.execute("PRAGMA foreign_keys = ON")
        start = detect_version(con)
        if start > CURRENT_SCHEMA_VERSION:
            raise MigrationError(
                "newer_than_supported",
                f"database schema version {start} is newer than supported "
                f"{CURRENT_SCHEMA_VERSION}",
            )
        if start == CURRENT_SCHEMA_VERSION:
            # Markers already claim current; the foundation ledger AND the current
            # registry schema must both back that claim before we agree there is
            # nothing to do. A forged/stale marker over a missing or malformed
            # endpoint_profiles registry fails closed here rather than serving it.
            _validate_current_ledger(con)
            _validate_current_schema(con)
            return {"status": "already_current", "from": start, "to": start, "applied": []}

        applied = []
        version = start
        for from_v, to_v, name, fn in _plan(MIGRATIONS, start):
            con.execute("BEGIN")
            try:
                fn(con)
                _record_migration(con, to_v, name, _now())
                con.execute(f"PRAGMA user_version = {int(to_v)}")
                updated = con.execute(
                    "UPDATE seed_meta SET value = ? WHERE key = 'schema_version'",
                    (str(to_v),),
                )
                if updated.rowcount != 1:
                    # The version mirror must move exactly one row; anything else
                    # would commit an inconsistent marker. Raise inside the
                    # transaction so the ledger and user_version changes roll back.
                    raise MigrationError(
                        "inconsistent_version",
                        f"seed_meta.schema_version mirror update touched "
                        f"{updated.rowcount} rows, expected exactly 1",
                    )
                # Each step must leave a ledger a later run would accept. The step
                # DDL is CREATE TABLE IF NOT EXISTS, so a stale or foreign
                # schema_migrations row can already be present; committing it
                # beside the new marker would then be rejected on the next run.
                # Validate the ledger PREFIX through this step's version inside the
                # transaction, so a mismatch (stray/foreign/missing row) rolls back
                # this step's ledger row and both marker changes instead of
                # committing a self-wedging state. The prefix -- not the full
                # current ledger -- is compared, so a legitimate intermediate step
                # is never measured against an impossible full-current ledger.
                _validate_ledger_prefix(con, to_v)
                if to_v == CURRENT_SCHEMA_VERSION:
                    # The CURRENT-reaching step must also leave a well-formed,
                    # fully-covered registry; validate inside the transaction so a
                    # broken table/backfill rolls back instead of committing a
                    # self-inconsistent v5.
                    _validate_current_schema(con)
                con.execute("COMMIT")
            except MigrationError:
                con.execute("ROLLBACK")
                raise
            except Exception as exc:  # any step failure -> atomic rollback
                con.execute("ROLLBACK")
                raise MigrationError(
                    "migration_failed",
                    f"migration {from_v}->{to_v} ({name}) failed and was rolled back: {exc}",
                ) from exc
            applied.append(name)
            version = to_v

        return {"status": "migrated", "from": start, "to": version, "applied": applied}
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply nondestructive, versioned forward migrations to the local "
            "synthetic SQLite demo database. Does not delete or reseed data."
        ),
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help="Path to the local synthetic SQLite database (default: demo-db/data/demo.sqlite).",
    )
    args = parser.parse_args(argv)

    try:
        result = migrate(Path(args.db))
    except MigrationError as exc:
        print(f"MIGRATE FAIL {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    print("MIGRATE PASS")
    print(f"status={result['status']}")
    print(f"from={result['from']}")
    print(f"to={result['to']}")
    print(f"applied={','.join(result['applied']) if result['applied'] else 'none'}")
    print(f"database={args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
