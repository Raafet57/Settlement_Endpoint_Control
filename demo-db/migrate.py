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
CURRENT_SCHEMA_VERSION = 5
# The pre-foundation generated schema (seed_meta.schema_version=3, user_version=0).
BASELINE_SCHEMA_VERSION = 3

# The single server-owned synthetic tenant. It is the tenant-ready extension
# point for the endpoint-profile registry; it is never client-supplied and no
# auth/tenant infrastructure is built on top of it. app.py mirrors this value.
SYNTHETIC_TENANT_ID = "synthetic-demo"

# The foundation step (3 -> 4): introduce the migration ledger + explicit version
# markers. Metadata only; no business table is created, altered, or dropped.
FOUNDATION_MIGRATION_NAME = "foundation_schema_migrations_ledger"
# The profile-registry step (4 -> 5): add the endpoint_profiles registry table
# and backfill one active profile per existing settlement endpoint. Additive
# only; no existing business row is altered or dropped.
PROFILES_MIGRATION_NAME = "endpoint_profiles_registry"

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


# Explicit, ordered, versioned registry. Each entry is
# (from_version, to_version, name, apply_fn). Add future steps in order.
MIGRATIONS = [
    (BASELINE_SCHEMA_VERSION, 4, FOUNDATION_MIGRATION_NAME, _migrate_3_to_4),
    (4, CURRENT_SCHEMA_VERSION, PROFILES_MIGRATION_NAME, _migrate_4_to_5),
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
