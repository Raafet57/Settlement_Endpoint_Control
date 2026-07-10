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
CURRENT_SCHEMA_VERSION = 4
# The pre-foundation generated schema (seed_meta.schema_version=3, user_version=0).
BASELINE_SCHEMA_VERSION = 3

# The one foundation step: introduce the migration ledger + explicit version
# markers. Metadata only; no business table is created, altered, or dropped.
FOUNDATION_MIGRATION_NAME = "foundation_schema_migrations_ledger"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


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


# Explicit, ordered, versioned registry. Each entry is
# (from_version, to_version, name, apply_fn). Add future steps in order.
MIGRATIONS = [
    (BASELINE_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION, FOUNDATION_MIGRATION_NAME, _migrate_3_to_4),
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


def _validate_current_ledger(con: sqlite3.Connection) -> None:
    """Fail closed unless the ledger exactly matches the expected current state.

    Version markers alone can be forged or left stale, so a database that claims
    to be at CURRENT must also carry the matching foundation ledger. Only the
    (version, name) rows are compared; the applied_at timestamp is free-form.
    """
    ledger = _read_current_ledger(con)
    expected = current_ledger()
    if ledger is None:
        raise MigrationError(
            "inconsistent_version",
            "version markers claim current but the schema_migrations ledger is "
            "missing or malformed",
        )
    if ledger != expected:
        raise MigrationError(
            "inconsistent_version",
            f"schema_migrations ledger {ledger} does not match the expected "
            f"current foundation {expected}",
        )


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
            # Markers already claim current; the foundation ledger must back that
            # claim before we agree there is nothing to do.
            _validate_current_ledger(con)
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
                if to_v == CURRENT_SCHEMA_VERSION:
                    # Reaching CURRENT must leave a ledger a later run would accept.
                    # The step DDL is CREATE TABLE IF NOT EXISTS, so a stale or
                    # foreign schema_migrations row can already be present;
                    # committing it beside the new marker would then be rejected by
                    # _validate_current_ledger() on the next run. Validate here,
                    # inside the transaction, so a mismatch rolls back this step's
                    # ledger row and both marker changes instead of committing a
                    # self-rejecting state. Only the CURRENT-reaching step is
                    # checked, so intermediate multi-step ledgers are never compared
                    # against an impossible full-current ledger.
                    _validate_current_ledger(con)
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
