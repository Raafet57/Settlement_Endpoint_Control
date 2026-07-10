#!/usr/bin/env python3
"""SEC-O10 focused tests for the nondestructive, versioned SQLite migration foundation.

These tests pin the contract of ``demo-db/migrate.py`` (the separate, explicit,
nondestructive migration command) and the fresh-fixture version produced by
``demo-db/seed.py`` (which stays the destructive deterministic reset command).

Every case runs against a **disposable temp SQLite database** and uses
**deliberately non-baseline** synthetic operator/audit row content (a fresh seed
produces zero operator_actions, so any operator_actions rows here are off-baseline)
to prove the migration preserves arbitrary existing local state rather than the
seed baseline. All fixtures are synthetic and localhost-only; no external calls.

Contract under test:

    | area          | input state                          | expected outcome                    |
    |---------------|--------------------------------------|-------------------------------------|
    | legacy detect | seed_meta=3, PRAGMA user_version=0    | resolves to version 3 (the one      |
    |               |                                      | supported legacy exception)         |
    | forward       | legacy v3 with custom operator/audit | upgrades to CURRENT; operator_actions|
    |               |                                      | and audit_events content/counts kept|
    | idempotent    | already at CURRENT                    | no-op, no duplicate ledger rows     |
    | fail-closed   | unknown / missing / newer / inconsistent | MigrationError, DB untouched     |
    | rollback      | a migration step that raises mid-way | atomic ROLLBACK: data + version      |
    |               |                                      | markers + schema unchanged          |
    | fresh seed    | seed.seed() into a temp DB           | born at CURRENT with ledger metadata|
    | CLI           | migrate.py --db PATH                  | 0 on success, nonzero on fail-closed|
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import migrate
import seed

ROOT = Path(__file__).resolve().parent
MIGRATE = ROOT / "migrate.py"

# A fixed synthetic timestamp; nothing here reads wall-clock time.
SYN_TIME = "2026-06-05T09:00:00Z"

# Deliberately non-baseline synthetic content. A fresh seed writes zero
# operator_actions, so these two rows can only come from persistent local state
# that the migration must preserve untouched.
OPERATOR_ROWS = [
    (1, 1, "open_repair_task", "ops_analyst", "recorded", "SYN-PRESERVE-operator-A", SYN_TIME),
    (2, 1, "hold_payment", "demo_operator", "recorded", "SYN-PRESERVE-operator-B", SYN_TIME),
]
AUDIT_ROWS = [
    (1, 1, 1, "logged", "Profile loaded", "SYN-PRESERVE-audit-1", SYN_TIME),
    (2, 1, 2, "action", "Repair task opened", "SYN-PRESERVE-audit-2", SYN_TIME),
    (3, 2, 1, "logged", "Profile loaded", "SYN-PRESERVE-audit-3", SYN_TIME),
    (4, 3, 1, "decision", "Policy decision", "SYN-PRESERVE-audit-4", SYN_TIME),
]

# The ledger a database at CURRENT_SCHEMA_VERSION must carry (timestamp free-form).
# Derived from the registry so it tracks every foundation step automatically.
GOOD_LEDGER = [(version, name, SYN_TIME) for (version, name) in migrate.current_ledger()]

# Every business table a v3->v4 foundation upgrade must leave byte-for-byte intact.
BUSINESS_TABLES = [
    "institutions", "legal_entities", "settlement_endpoints", "route_policies",
    "demo_scenarios", "policy_checks", "route_decisions", "audit_events",
    "operator_actions", "source_manifest",
]

# Minimal standalone shapes mirroring seed.py's columns for the tables whose
# preservation the invariants name, plus a minimal settlement_endpoints (a v3
# baseline business table the 4->5 profile-registry backfill reads). No extra FK
# REFERENCES: the fixture is a disposable stand-in for persistent local state,
# not a full seeded graph. Two synthetic endpoints prove the backfill creates one
# active profile per endpoint.
_FIXTURE_DDL = """
CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE settlement_endpoints (id INTEGER PRIMARY KEY);
INSERT INTO settlement_endpoints(id) VALUES (1), (2);
CREATE TABLE operator_actions (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL,
    display_order INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def build_db(
    path: Path,
    *,
    user_version: int,
    seed_meta: dict[str, str] | None,
    operator_rows=OPERATOR_ROWS,
    audit_rows=AUDIT_ROWS,
    ledger_rows=None,
) -> None:
    """Create a disposable synthetic SQLite DB in a chosen version state.

    ``ledger_rows`` controls the schema_migrations foundation ledger:
      * ``None`` -> no schema_migrations table at all
      * ``[]``   -> table present but empty
      * list of (version, name, applied_at) -> table with exactly those rows
    """
    con = sqlite3.connect(path)
    try:
        con.executescript(_FIXTURE_DDL)
        if ledger_rows is not None:
            con.execute(migrate.SCHEMA_MIGRATIONS_DDL)
            con.executemany(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                ledger_rows,
            )
        con.executemany(
            "INSERT INTO operator_actions(id, scenario_id, action_type, actor, status, detail, created_at) VALUES (?,?,?,?,?,?,?)",
            operator_rows,
        )
        con.executemany(
            "INSERT INTO audit_events(id, scenario_id, display_order, event_type, title, detail, created_at) VALUES (?,?,?,?,?,?,?)",
            audit_rows,
        )
        if seed_meta is not None:
            con.executemany(
                "INSERT INTO seed_meta(key, value) VALUES (?,?)",
                list(seed_meta.items()),
            )
        con.execute(f"PRAGMA user_version = {int(user_version)}")
        con.commit()
    finally:
        con.close()


def create_current_repair_tables(con: sqlite3.Connection) -> None:
    """Materialize the exact current (v6) SEC-P30 decision/repair/event tables.

    A fixture that stamps CURRENT must carry the WHOLE current registry, not only
    the v5 endpoint_profiles table, or fail-closed current-schema validation
    (rightly) rejects it as not actually current. Uses the real module DDL so the
    tables are byte-for-byte the declared current shape.
    """
    con.executescript(migrate.PROFILE_DECISIONS_DDL)
    con.executescript(migrate.REPAIR_TASKS_DDL)
    con.executescript(migrate.REPAIR_TASKS_OPEN_INDEX_DDL)
    con.executescript(migrate.REPAIR_EVENTS_DDL)


def legacy_v3(path: Path, **kwargs) -> None:
    """The real legacy state: seed_meta.schema_version=3 with PRAGMA user_version=0."""
    build_db(
        path,
        user_version=0,
        seed_meta={"schema_version": "3", "fixture_boundary": "synthetic_data_only"},
        **kwargs,
    )


def read_markers(path: Path) -> tuple[int, str | None]:
    con = sqlite3.connect(path)
    try:
        uv = con.execute("PRAGMA user_version").fetchone()[0]
        row = con.execute("SELECT value FROM seed_meta WHERE key='schema_version'").fetchone()
        return uv, (row[0] if row else None)
    finally:
        con.close()


def read_rows(path: Path, table: str, order_by: str = "id") -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return [tuple(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY {order_by}")]
    finally:
        con.close()


def table_exists(path: Path, name: str) -> bool:
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    finally:
        con.close()


def business_snapshot(path: Path) -> dict:
    """Full content of every business table, keyed by table name."""
    con = sqlite3.connect(path)
    try:
        return {
            table: con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in BUSINESS_TABLES
        }
    finally:
        con.close()


class TempDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sec_o10_")
        self.addCleanup(self._tmp.cleanup)  # removes every generated temp DB
        self.tmpdir = Path(self._tmp.name)

    def db(self, name: str = "demo.sqlite") -> Path:
        return self.tmpdir / name


class DetectVersionTests(TempDbTestCase):
    def test_legacy_seed_meta3_user_version0_resolves_to_3(self) -> None:
        path = self.db()
        legacy_v3(path)
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        self.assertEqual(migrate.detect_version(con), 3)

    def test_consistent_current_markers_resolve_to_current(self) -> None:
        path = self.db()
        build_db(
            path,
            user_version=migrate.CURRENT_SCHEMA_VERSION,
            seed_meta={"schema_version": str(migrate.CURRENT_SCHEMA_VERSION)},
            ledger_rows=[],  # detect_version resolves from markers alone
        )
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        self.assertEqual(migrate.detect_version(con), migrate.CURRENT_SCHEMA_VERSION)

    def test_unknown_version_fails_closed(self) -> None:
        for bad in ("99", "2", "4"):  # user_version=0 with any non-legacy seed_meta
            with self.subTest(seed_meta=bad):
                path = self.db(f"unknown_{bad}.sqlite")
                build_db(path, user_version=0, seed_meta={"schema_version": bad})
                con = sqlite3.connect(path)
                self.addCleanup(con.close)
                with self.assertRaises(migrate.MigrationError) as cm:
                    migrate.detect_version(con)
                self.assertEqual(cm.exception.code, "unknown_version")

    def test_missing_seed_meta_fails_closed(self) -> None:
        path = self.db()
        build_db(path, user_version=0, seed_meta=None)  # no seed_meta rows at all
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.detect_version(con)
        self.assertEqual(cm.exception.code, "unknown_version")

    def test_inconsistent_markers_fail_closed(self) -> None:
        path = self.db()
        build_db(path, user_version=4, seed_meta={"schema_version": "3"})  # disagree, not legacy
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.detect_version(con)
        self.assertEqual(cm.exception.code, "inconsistent_version")


class MigrateRunnerTests(TempDbTestCase):
    def test_v3_to_current_preserves_operator_and_audit(self) -> None:
        path = self.db()
        legacy_v3(path)

        result = migrate.migrate(path)

        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["from"], 3)
        self.assertEqual(result["to"], migrate.CURRENT_SCHEMA_VERSION)
        # The full forward path applies every registered step in order.
        self.assertEqual(result["applied"], [name for (_v, name) in migrate.current_ledger()])

        # Version markers advanced and now consistent.
        uv, sm = read_markers(path)
        self.assertEqual(uv, migrate.CURRENT_SCHEMA_VERSION)
        self.assertEqual(sm, str(migrate.CURRENT_SCHEMA_VERSION))

        # Ledger metadata recorded.
        ledger = read_rows(path, "schema_migrations", order_by="version")
        self.assertEqual([(v, n) for (v, n, _at) in ledger], migrate.current_ledger())

        # The 4->5 step backfilled one active profile per settlement endpoint,
        # under the server-owned tenant, with no supersession link.
        profiles = read_rows(path, "endpoint_profiles", order_by="id")
        self.assertEqual([(p[1], p[2], p[3], p[4]) for p in profiles], [
            (migrate.SYNTHETIC_TENANT_ID, 1, "active", None),
            (migrate.SYNTHETIC_TENANT_ID, 2, "active", None),
        ])

        # Non-baseline synthetic data preserved exactly (content AND counts).
        self.assertEqual(read_rows(path, "operator_actions"), OPERATOR_ROWS)
        self.assertEqual(read_rows(path, "audit_events"), AUDIT_ROWS)

        # Unrelated seed_meta keys left untouched.
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        boundary = con.execute("SELECT value FROM seed_meta WHERE key='fixture_boundary'").fetchone()
        self.assertEqual(boundary[0], "synthetic_data_only")

    def test_migrate_is_idempotent(self) -> None:
        path = self.db()
        legacy_v3(path)

        first = migrate.migrate(path)
        second = migrate.migrate(path)

        self.assertEqual(first["status"], "migrated")
        self.assertEqual(second["status"], "already_current")
        self.assertEqual(second["from"], migrate.CURRENT_SCHEMA_VERSION)
        self.assertEqual(second["to"], migrate.CURRENT_SCHEMA_VERSION)
        self.assertEqual(second["applied"], [])

        # No duplicate ledger rows and data still intact after a second pass.
        self.assertEqual(len(read_rows(path, "schema_migrations", order_by="version")), len(migrate.current_ledger()))
        self.assertEqual(read_rows(path, "operator_actions"), OPERATOR_ROWS)
        self.assertEqual(read_rows(path, "audit_events"), AUDIT_ROWS)

    def test_newer_than_supported_fails_closed(self) -> None:
        path = self.db()
        newer = migrate.CURRENT_SCHEMA_VERSION + 1
        build_db(path, user_version=newer, seed_meta={"schema_version": str(newer)}, ledger_rows=[])
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.migrate(path)
        self.assertEqual(cm.exception.code, "newer_than_supported")
        # Untouched.
        self.assertEqual(read_markers(path), (newer, str(newer)))
        self.assertEqual(read_rows(path, "operator_actions"), OPERATOR_ROWS)

    def test_missing_database_fails_closed(self) -> None:
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.migrate(self.db("does_not_exist.sqlite"))
        self.assertEqual(cm.exception.code, "no_database")

    def test_failed_migration_rolls_back_atomically(self) -> None:
        path = self.db()
        legacy_v3(path)

        def boom(con: sqlite3.Connection) -> None:
            # Mutate data, version markers, and schema, then fail. All of this
            # must be reverted atomically by the runner's ROLLBACK.
            con.execute("DELETE FROM operator_actions")
            con.execute("DELETE FROM audit_events")
            con.execute("PRAGMA user_version = 999")
            con.execute("UPDATE seed_meta SET value='999' WHERE key='schema_version'")
            con.execute("CREATE TABLE probe_should_not_survive (x)")
            raise RuntimeError("induced migration failure")

        failing = [(3, migrate.CURRENT_SCHEMA_VERSION, "boom_step", boom)]
        # migrate() always uses the explicit module registry; patch it rather
        # than injecting a private parameter into the public surface.
        with mock.patch.object(migrate, "MIGRATIONS", failing):
            with self.assertRaises(migrate.MigrationError) as cm:
                migrate.migrate(path)
        self.assertEqual(cm.exception.code, "migration_failed")

        # Version markers unchanged (still legacy).
        self.assertEqual(read_markers(path), (0, "3"))
        # Data unchanged (content AND counts).
        self.assertEqual(read_rows(path, "operator_actions"), OPERATOR_ROWS)
        self.assertEqual(read_rows(path, "audit_events"), AUDIT_ROWS)
        # Schema effects of the failed step rolled back.
        self.assertFalse(table_exists(path, "probe_should_not_survive"))
        self.assertFalse(table_exists(path, "schema_migrations"))

    def test_empty_registry_cannot_report_already_current_at_v3(self) -> None:
        # An accidentally empty registry must not let a v3 DB report
        # already_current while still below CURRENT: _plan must fail closed on any
        # end-version other than CURRENT, including an empty plan.
        path = self.db()
        legacy_v3(path)
        with mock.patch.object(migrate, "MIGRATIONS", []):
            with self.assertRaises(migrate.MigrationError) as cm:
                migrate.migrate(path)
        self.assertEqual(cm.exception.code, "unknown_version")
        # A DB that cannot be advanced is left exactly as-is.
        self.assertEqual(read_markers(path), (0, "3"))
        self.assertEqual(read_rows(path, "operator_actions"), OPERATOR_ROWS)

    def test_seed_meta_mirror_update_must_touch_exactly_one_row(self) -> None:
        # If the seed_meta.schema_version mirror row is not moved by exactly one
        # UPDATE, the runner must refuse to commit an inconsistent mirror and roll
        # the whole step back (ledger + user_version included).
        path = self.db()
        legacy_v3(path)

        def drop_mirror(con: sqlite3.Connection) -> None:
            con.execute(migrate.SCHEMA_MIGRATIONS_DDL)  # legitimate ledger creation
            con.execute("DELETE FROM seed_meta WHERE key='schema_version'")  # sabotage mirror

        sabotage = [(3, migrate.CURRENT_SCHEMA_VERSION, "drop_mirror_step", drop_mirror)]
        with mock.patch.object(migrate, "MIGRATIONS", sabotage):
            with self.assertRaises(migrate.MigrationError) as cm:
                migrate.migrate(path)
        self.assertEqual(cm.exception.code, "inconsistent_version")
        # Rolled back atomically: ledger absent, mirror + markers restored.
        self.assertFalse(table_exists(path, "schema_migrations"))
        self.assertEqual(read_markers(path), (0, "3"))
        self.assertEqual(read_rows(path, "operator_actions"), OPERATOR_ROWS)

    def test_v3_upgrade_with_unexpected_pre_existing_ledger_fails_closed(self) -> None:
        # A legacy v3 marker DB can already hold a stray schema_migrations row (the
        # foundation DDL is CREATE TABLE IF NOT EXISTS, so a foreign/earlier row
        # survives into the step). Applying the foundation would append the v4 row
        # on top of it and commit a ledger that the very next run's
        # _validate_current_ledger() rejects -- a self-wedging false success. The
        # runner must instead validate the final ledger inside the transaction and
        # fail closed, rolling back the new ledger row and both marker changes.
        path = self.db()
        stray = (migrate.BASELINE_SCHEMA_VERSION, "unexpected_legacy_step", SYN_TIME)
        legacy_v3(path, ledger_rows=[stray])

        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.migrate(path)
        self.assertEqual(cm.exception.code, "inconsistent_version")

        # Rolled back atomically: version markers stay at the legacy state.
        self.assertEqual(read_markers(path), (0, "3"))
        # The pre-existing ledger row is preserved and no CURRENT (v4) row survives.
        ledger = read_rows(path, "schema_migrations", order_by="version")
        self.assertEqual(ledger, [stray])
        self.assertNotIn(migrate.CURRENT_SCHEMA_VERSION, [v for (v, _n, _at) in ledger])
        # Non-baseline synthetic data preserved exactly (content AND counts).
        self.assertEqual(read_rows(path, "operator_actions"), OPERATOR_ROWS)
        self.assertEqual(read_rows(path, "audit_events"), AUDIT_ROWS)

    def test_cli_success_returns_zero(self) -> None:
        path = self.db()
        legacy_v3(path)
        proc = subprocess.run(
            [sys.executable, str(MIGRATE), "--db", str(path)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MIGRATE PASS", proc.stdout)
        self.assertEqual(read_markers(path), (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)))
        self.assertEqual(read_rows(path, "operator_actions"), OPERATOR_ROWS)

    def test_cli_fail_closed_returns_nonzero(self) -> None:
        path = self.db()
        build_db(path, user_version=0, seed_meta={"schema_version": "99"})
        proc = subprocess.run(
            [sys.executable, str(MIGRATE), "--db", str(path)],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown_version", proc.stderr)
        # DB left untouched by a fail-closed run.
        self.assertEqual(read_markers(path), (0, "99"))


class FreshSeedVersionTests(TempDbTestCase):
    def test_fresh_seed_is_current_version_with_ledger(self) -> None:
        path = self.db("seed.sqlite")
        with mock.patch.object(seed, "DB_PATH", path):
            counts = seed.seed()

        # Fresh seed represents the new current schema/version.
        uv, sm = read_markers(path)
        self.assertEqual(uv, migrate.CURRENT_SCHEMA_VERSION)
        self.assertEqual(sm, str(migrate.CURRENT_SCHEMA_VERSION))

        # Migration metadata present and matching the registry end-state.
        ledger = read_rows(path, "schema_migrations", order_by="version")
        self.assertEqual([(v, n) for (v, n, _at) in ledger], migrate.current_ledger())
        self.assertEqual(counts["schema_migrations"], len(migrate.current_ledger()))

        # Baseline is genuinely off the preserved-content fixture: fresh seed
        # writes zero operator actions.
        self.assertEqual(counts["operator_actions"], 0)

    def test_fresh_seed_then_migrate_is_noop(self) -> None:
        path = self.db("seed.sqlite")
        with mock.patch.object(seed, "DB_PATH", path):
            seed.seed()
        result = migrate.migrate(path)
        self.assertEqual(result["status"], "already_current")
        self.assertEqual(result["applied"], [])


class CurrentLedgerValidationTests(TempDbTestCase):
    """A DB whose markers claim CURRENT must also carry the matching ledger."""

    def test_current_markers_with_valid_ledger_report_already_current(self) -> None:
        path = self.db()
        build_db(
            path,
            user_version=migrate.CURRENT_SCHEMA_VERSION,
            seed_meta={"schema_version": str(migrate.CURRENT_SCHEMA_VERSION)},
            ledger_rows=GOOD_LEDGER,
        )
        # A DB claiming CURRENT must also carry a well-formed, fully-covered
        # endpoint_profiles registry AND the v6 decision/repair/event tables for
        # already_current to hold.
        con = sqlite3.connect(path)
        con.executescript(migrate.ENDPOINT_PROFILES_DDL)
        migrate.backfill_active_profiles(con, migrate.SYNTHETIC_TENANT_ID, SYN_TIME)
        create_current_repair_tables(con)
        con.commit()
        con.close()
        result = migrate.migrate(path)
        self.assertEqual(result["status"], "already_current")
        self.assertEqual(result["applied"], [])

    def test_current_markers_with_bad_ledger_fail_closed(self) -> None:
        bad_ledgers = {
            "missing_table": None,
            "empty": [],
            "wrong_name": [(migrate.CURRENT_SCHEMA_VERSION, "not_the_foundation_step", SYN_TIME)],
            "wrong_version": [(migrate.BASELINE_SCHEMA_VERSION, migrate.FOUNDATION_MIGRATION_NAME, SYN_TIME)],
            "extra_row": GOOD_LEDGER + [(migrate.CURRENT_SCHEMA_VERSION + 1, "phantom_step", SYN_TIME)],
        }
        for label, rows in bad_ledgers.items():
            with self.subTest(ledger=label):
                path = self.db(f"badledger_{label}.sqlite")
                build_db(
                    path,
                    user_version=migrate.CURRENT_SCHEMA_VERSION,
                    seed_meta={"schema_version": str(migrate.CURRENT_SCHEMA_VERSION)},
                    ledger_rows=rows,
                )
                with self.assertRaises(migrate.MigrationError) as cm:
                    migrate.migrate(path)
                self.assertEqual(cm.exception.code, "inconsistent_version")
                # Fail-closed leaves markers untouched.
                self.assertEqual(
                    read_markers(path),
                    (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)),
                )

    def test_current_markers_with_malformed_ledger_fail_closed(self) -> None:
        path = self.db("malformed_ledger.sqlite")
        build_db(
            path,
            user_version=migrate.CURRENT_SCHEMA_VERSION,
            seed_meta={"schema_version": str(migrate.CURRENT_SCHEMA_VERSION)},
        )
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE schema_migrations (unexpected_column TEXT)")

        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.migrate(path)

        self.assertEqual(cm.exception.code, "inconsistent_version")
        self.assertEqual(
            read_markers(path),
            (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)),
        )


class FullSchemaUpgradeTests(TempDbTestCase):
    """Exercise the REAL seeded business schema, not the reduced fixture.

    v3 -> v4 is intentionally metadata-only: it adds the schema_migrations ledger
    and version markers and must leave every business table byte-for-byte intact.
    """

    def test_full_business_schema_v3_upgrade_preserves_everything(self) -> None:
        path = self.db("full.sqlite")
        with mock.patch.object(seed, "DB_PATH", path):
            seed.seed()

        # Deliberately non-baseline synthetic rows: a fresh seed writes zero
        # operator actions, so these can only be persistent local state.
        con = sqlite3.connect(path)
        con.execute(
            "INSERT INTO operator_actions(scenario_id, action_type, actor, status, detail, created_at)"
            " VALUES (1,'open_repair_task','ops_analyst','recorded','SYN-FULL-op-A','2026-06-05T09:00:00Z')"
        )
        con.execute(
            "INSERT INTO operator_actions(scenario_id, action_type, actor, status, detail, created_at)"
            " VALUES (2,'hold_payment','demo_operator','recorded','SYN-FULL-op-B','2026-06-05T09:00:00Z')"
        )
        con.execute(
            "INSERT INTO audit_events(scenario_id, display_order, event_type, title, detail, created_at)"
            " VALUES (1,99,'action','Repair task opened','SYN-FULL-audit','2026-06-05T09:00:00Z')"
        )
        # Convert ONLY foundation metadata back to the real shipped v3 state.
        con.execute("DROP TABLE schema_migrations")
        con.execute("UPDATE seed_meta SET value='3' WHERE key='schema_version'")
        con.execute("PRAGMA user_version = 0")
        con.commit()
        con.close()

        before = business_snapshot(path)
        self.assertEqual(len(before["operator_actions"]), 2)  # confirm off-baseline

        result = migrate.migrate(path)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["to"], migrate.CURRENT_SCHEMA_VERSION)

        # Every business table unchanged (counts AND content).
        self.assertEqual(business_snapshot(path), before)

        # Structural integrity intact after the metadata-only upgrade.
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        # Foundation now in place and consistent.
        self.assertEqual(
            read_markers(path),
            (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)),
        )
        ledger = read_rows(path, "schema_migrations", order_by="version")
        self.assertEqual([(v, n) for (v, n, _at) in ledger], migrate.current_ledger())


class EndpointProfilesV5MigrationTests(TempDbTestCase):
    """SEC-P20: the real 4 -> 5 profile-registry migration.

    Exercises table creation, per-endpoint active-profile backfill, idempotence,
    atomic rollback, foreign-key/integrity checks, and byte-for-byte preservation
    of every existing business row -- against the FULL seeded business schema.
    """

    def _seed_full(self, path: Path) -> None:
        with mock.patch.object(seed, "DB_PATH", path):
            seed.seed()

    def _downgrade(self, path: Path, to_version: int) -> None:
        """Turn a fresh CURRENT seed into a genuine earlier state.

        A genuine pre-v5 state carries none of the tables or ledger rows added at
        version 5 or later. The profiles registry is version 5 and the repair /
        decision tables are version 6, so the ledger rows to strip are every row
        with ``version > to_version`` -- NOT merely the single CURRENT row. Deleting
        only ``version == CURRENT`` would leave a stale version-5 ledger row that
        collides (UNIQUE(schema_migrations.version)) when the real 4->5 step records
        version 5 again.
        """
        con = sqlite3.connect(path)
        try:
            # v6 decision/repair tables (v5+) and the v5 endpoint_profiles registry.
            con.execute("DROP TABLE IF EXISTS repair_events")
            con.execute("DROP TABLE IF EXISTS repair_tasks")
            con.execute("DROP TABLE IF EXISTS profile_decisions")
            con.execute("DROP TABLE endpoint_profiles")
            if to_version == 4:
                con.execute("DELETE FROM schema_migrations WHERE version > 4")
                con.execute("UPDATE seed_meta SET value='4' WHERE key='schema_version'")
                con.execute("PRAGMA user_version = 4")
            else:  # a true legacy v3: no ledger, user_version unset
                con.execute("DROP TABLE schema_migrations")
                con.execute("UPDATE seed_meta SET value='3' WHERE key='schema_version'")
                con.execute("PRAGMA user_version = 0")
            con.commit()
        finally:
            con.close()

    def _assert_backfill_and_integrity(self, path: Path) -> None:
        profiles = read_rows(path, "endpoint_profiles", order_by="id")
        self.assertEqual(len(profiles), 1, "one active profile per settlement endpoint")
        self.assertEqual(
            (profiles[0][1], profiles[0][2], profiles[0][3], profiles[0][4]),
            (migrate.SYNTHETIC_TENANT_ID, 1, "active", None),
        )
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_v4_to_v5_creates_table_and_backfills(self) -> None:
        path = self.db("v4.sqlite")
        self._seed_full(path)
        self._downgrade(path, 4)
        self.assertFalse(table_exists(path, "endpoint_profiles"))
        before = business_snapshot(path)

        result = migrate.migrate(path)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["from"], 4)
        self.assertEqual(result["to"], migrate.CURRENT_SCHEMA_VERSION)
        # From a genuine v4 the runner traverses the profiles step (5) and then the
        # repair/decision step (6) to reach CURRENT; the profiles-registry table
        # creation and backfill asserted below prove the v5 step specifically ran.
        self.assertEqual(
            result["applied"],
            [migrate.PROFILES_MIGRATION_NAME, migrate.REPAIR_DECISIONS_MIGRATION_NAME],
        )

        self.assertTrue(table_exists(path, "endpoint_profiles"))
        self._assert_backfill_and_integrity(path)
        # Every business table is byte-for-byte identical (additive migration).
        self.assertEqual(business_snapshot(path), before)
        self.assertEqual(read_markers(path), (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)))

    def test_v4_to_v5_is_idempotent(self) -> None:
        path = self.db("v4b.sqlite")
        self._seed_full(path)
        self._downgrade(path, 4)
        first = migrate.migrate(path)
        second = migrate.migrate(path)
        self.assertEqual(first["status"], "migrated")
        self.assertEqual(second["status"], "already_current")
        self.assertEqual(second["applied"], [])
        # No duplicate backfill on a second pass.
        self.assertEqual(len(read_rows(path, "endpoint_profiles")), 1)

    def test_v4_to_v5_rolls_back_on_induced_failure(self) -> None:
        path = self.db("v4c.sqlite")
        self._seed_full(path)
        self._downgrade(path, 4)

        def boom(con: sqlite3.Connection) -> None:
            # Create the table and a partial backfill, then fail: the runner must
            # revert the table, the row, and both version markers atomically.
            con.execute(migrate.ENDPOINT_PROFILES_DDL)
            con.execute(
                "INSERT INTO endpoint_profiles(tenant_id, endpoint_id, lifecycle_state, superseded_by, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (migrate.SYNTHETIC_TENANT_ID, 1, "active", None, SYN_TIME, SYN_TIME),
            )
            raise RuntimeError("induced 4->5 failure")

        failing = [(4, migrate.CURRENT_SCHEMA_VERSION, "boom_profiles", boom)]
        with mock.patch.object(migrate, "MIGRATIONS", failing):
            with self.assertRaises(migrate.MigrationError) as cm:
                migrate.migrate(path)
        self.assertEqual(cm.exception.code, "migration_failed")
        self.assertFalse(table_exists(path, "endpoint_profiles"), "table creation must roll back")
        self.assertEqual(read_markers(path), (4, "4"), "version markers must stay at v4")

    def test_full_path_v3_to_v5_backfills_and_preserves(self) -> None:
        path = self.db("v3full.sqlite")
        self._seed_full(path)
        # A non-baseline synthetic operator action a fresh seed never writes, so it
        # can only be preserved persistent local state.
        con = sqlite3.connect(path)
        con.execute(
            "INSERT INTO operator_actions(scenario_id, action_type, actor, status, detail, created_at)"
            " VALUES (1,'hold_payment','ops_analyst','recorded','SYN-P20-preserve','2026-06-05T09:00:00Z')"
        )
        con.commit()
        con.close()
        self._downgrade(path, 3)
        before = business_snapshot(path)

        result = migrate.migrate(path)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["from"], 3)
        self.assertEqual(result["to"], migrate.CURRENT_SCHEMA_VERSION)
        self.assertEqual(result["applied"], [name for (_v, name) in migrate.current_ledger()])

        self._assert_backfill_and_integrity(path)
        self.assertEqual(business_snapshot(path), before)
        self.assertEqual(len(read_rows(path, "operator_actions")), 1)

    def test_current_schema_validated_inside_v5_migration_transaction(self) -> None:
        # A v5-reaching step that creates the table but leaves an endpoint
        # uncovered must be caught INSIDE the migration transaction and rolled
        # back (table gone, markers stay v4), not committed as a broken v5.
        path = self.db("v4txn.sqlite")
        self._seed_full(path)
        self._downgrade(path, 4)

        def create_only(con: sqlite3.Connection) -> None:
            con.execute(migrate.ENDPOINT_PROFILES_DDL)  # create table, skip the backfill

        step = [(4, migrate.CURRENT_SCHEMA_VERSION, migrate.PROFILES_MIGRATION_NAME, create_only)]
        with mock.patch.object(migrate, "MIGRATIONS", step):
            with self.assertRaises(migrate.MigrationError) as cm:
                migrate.migrate(path)
        self.assertEqual(cm.exception.code, "inconsistent_version")
        self.assertFalse(table_exists(path, "endpoint_profiles"), "the broken v5 step must roll back")
        self.assertEqual(read_markers(path), (4, "4"))


# Alternate endpoint_profiles table shapes used to inject exactly one current-
# schema defect while markers and the ledger stay valid.
_FK_ONLY_PROFILES_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL,
    superseded_by INTEGER REFERENCES endpoint_profiles(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_MISSING_COLUMN_PROFILES_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_NO_FK_PROFILES_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL,
    lifecycle_state TEXT NOT NULL,
    superseded_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
# Claimed-v5 tables that carry valid-looking rows but each DROP exactly one
# declared rail, so a future write could violate an invariant the current data
# happens to satisfy. Each keeps every OTHER named rail intact so the validator
# must fail on the single missing one.
_NO_UNIQUE_ENDPOINT_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('draft','active','superseded')),
    superseded_by INTEGER REFERENCES endpoint_profiles(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_NO_LIFECYCLE_CHECK_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL,
    superseded_by INTEGER REFERENCES endpoint_profiles(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_NO_SELF_FK_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('draft','active','superseded')),
    superseded_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_NULLABLE_COLUMN_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('draft','active','superseded')),
    superseded_by INTEGER REFERENCES endpoint_profiles(id),
    created_at TEXT,
    updated_at TEXT NOT NULL
);
"""
_NO_PK_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER NOT NULL,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('draft','active','superseded')),
    superseded_by INTEGER REFERENCES endpoint_profiles(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
# Composite PRIMARY KEY(id, tenant_id): id still has PK ordinal 1 (so a pk-flag
# check passes) but the declared identity is not the exact single-column id PK.
_COMPOSITE_PK_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('draft','active','superseded')),
    superseded_by INTEGER UNIQUE REFERENCES endpoint_profiles(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id, tenant_id),
    CHECK (
        (lifecycle_state IN ('draft','active') AND superseded_by IS NULL)
        OR (lifecycle_state = 'superseded' AND superseded_by IS NOT NULL AND superseded_by <> id)
    )
);
"""
# Lifecycle enum CHECK kept, but the lifecycle/superseded_by relationship CHECK
# is dropped entirely.
_NO_RELATIONSHIP_CHECK_DDL = """
CREATE TABLE endpoint_profiles (
    id INTEGER PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(id),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('draft','active','superseded')),
    superseded_by INTEGER UNIQUE REFERENCES endpoint_profiles(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class CurrentSchemaValidationTests(TempDbTestCase):
    """Valid v5 markers + ledger are not enough: migrate() must also verify the
    endpoint_profiles registry exists, has the required shape and foreign key,
    covers every settlement endpoint exactly once, and has valid lifecycle and
    superseded_by relationships -- failing closed and preserving state otherwise.
    """

    def _v5(self, name, setup=None):
        path = self.db(name)
        build_db(
            path,
            user_version=migrate.CURRENT_SCHEMA_VERSION,
            seed_meta={"schema_version": str(migrate.CURRENT_SCHEMA_VERSION)},
            ledger_rows=GOOD_LEDGER,
        )
        if setup is not None:
            con = sqlite3.connect(path)  # foreign_keys OFF by default -> inject freely
            try:
                setup(con)
                con.commit()
            finally:
                con.close()
        return path

    @staticmethod
    def _insert(con, rows):
        con.executemany(
            "INSERT INTO endpoint_profiles(id, tenant_id, endpoint_id, lifecycle_state, superseded_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )

    def _assert_fails_closed(self, path):
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.migrate(path)
        self.assertEqual(cm.exception.code, "inconsistent_version")
        # State preserved: markers stay at the (claimed) current version.
        self.assertEqual(read_markers(path), (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)))

    def test_valid_v5_reports_already_current(self):
        def setup(con):
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            migrate.backfill_active_profiles(con, migrate.SYNTHETIC_TENANT_ID, SYN_TIME)
            create_current_repair_tables(con)
        path = self._v5("valid.sqlite", setup)
        self.assertEqual(migrate.migrate(path)["status"], "already_current")

    def test_missing_current_table_fails_closed(self):
        self._assert_fails_closed(self._v5("missing.sqlite", setup=None))

    def test_malformed_current_table_fails_closed(self):
        def setup(con):
            con.executescript(_MISSING_COLUMN_PROFILES_DDL)  # no superseded_by column
        self._assert_fails_closed(self._v5("malformed.sqlite", setup))

    def test_missing_foreign_key_fails_closed(self):
        def setup(con):
            con.executescript(_NO_FK_PROFILES_DDL)
            self._insert(con, [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "active", None, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
            ])
        self._assert_fails_closed(self._v5("nofk.sqlite", setup))

    def test_uncovered_endpoint_fails_closed(self):
        def setup(con):
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            self._insert(con, [(1, migrate.SYNTHETIC_TENANT_ID, 1, "active", None, SYN_TIME, SYN_TIME)])  # endpoint 2 orphaned
        self._assert_fails_closed(self._v5("uncovered.sqlite", setup))

    def test_duplicate_coverage_fails_closed(self):
        def setup(con):
            con.executescript(_FK_ONLY_PROFILES_DDL)  # no UNIQUE(endpoint_id)
            self._insert(con, [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "active", None, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
                (3, migrate.SYNTHETIC_TENANT_ID, 1, "draft", None, SYN_TIME, SYN_TIME),  # endpoint 1 twice
            ])
        self._assert_fails_closed(self._v5("dupe.sqlite", setup))

    def test_invalid_lifecycle_link_fails_closed(self):
        for label, rows in {
            "draft_with_link": [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "draft", 2, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
            ],
            "superseded_without_link": [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "superseded", None, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
            ],
            "superseded_self_link": [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "superseded", 1, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
            ],
            "unknown_lifecycle": [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "archived", None, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
            ],
            "null_lifecycle": [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, None, None, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
            ],
        }.items():
            with self.subTest(case=label):
                def setup(con, rows=rows):
                    ddl = _FK_ONLY_PROFILES_DDL
                    if label == "null_lifecycle":
                        ddl = ddl.replace("lifecycle_state TEXT NOT NULL", "lifecycle_state TEXT")
                    con.executescript(ddl)  # no CHECK -> inject the bad row
                    self._insert(con, rows)
                self._assert_fails_closed(self._v5(f"link_{label}.sqlite", setup))

    def test_missing_settlement_endpoints_table_fails_closed_stably(self):
        # A well-formed endpoint_profiles registry but an absent (unreadable)
        # settlement_endpoints table must convert the raw sqlite3.OperationalError
        # into a stable inconsistent_version MigrationError, not a traceback.
        def setup(con):
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            self._insert(con, [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "active", None, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
            ])
            con.execute("DROP TABLE settlement_endpoints")
        self._assert_fails_closed(self._v5("no_endpoints.sqlite", setup))

    def test_cli_missing_settlement_endpoints_is_clean_fail_closed(self):
        def setup(con):
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            con.execute("DROP TABLE settlement_endpoints")
        path = self._v5("cli_no_endpoints.sqlite", setup)
        proc = subprocess.run(
            [sys.executable, str(MIGRATE), "--db", str(path)], text=True, capture_output=True
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr, "the CLI must fail closed without a traceback")
        self.assertIn("inconsistent_version", proc.stderr)

    def test_wrong_tenant_profiles_fail_closed(self):
        # Every row is well-formed and covers an endpoint exactly once, but under a
        # tenant other than the server-owned one -- so the app would see no
        # profiles. The validator must reject this as inconsistent, not serve it.
        def setup(con):
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            self._insert(con, [
                (1, "attacker-tenant", 1, "active", None, SYN_TIME, SYN_TIME),
                (2, "attacker-tenant", 2, "active", None, SYN_TIME, SYN_TIME),
            ])
        self._assert_fails_closed(self._v5("wrong_tenant.sqlite", setup))

    # -- SEC-P20 blocker 2: current-state supersession-history invariants that no
    # per-row CHECK can see (the CHECK cannot inspect the target's state, and
    # cannot span rows) yet must hold for a well-formed v5 registry.

    def test_superseded_by_draft_target_fails_closed(self):
        # A superseded profile whose replacement is still a draft is impossible
        # through the state machine (supersession activates the replacement
        # atomically), but the per-row CHECK cannot see the target's lifecycle.
        def setup(con):
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            self._insert(con, [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "superseded", 2, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "draft", None, SYN_TIME, SYN_TIME),  # a draft target
            ])
        self._assert_fails_closed(self._v5("draft_target.sqlite", setup))

    def test_supersession_cycle_fails_closed(self):
        # A multi-profile supersession cycle (1 -> 2 -> 1) satisfies every per-row
        # rule (each link is non-null and non-self) but is a corrupt history with
        # no active head.
        def setup(con):
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            self._insert(con, [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "superseded", 2, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "superseded", 1, SYN_TIME, SYN_TIME),
            ])
        self._assert_fails_closed(self._v5("cycle.sqlite", setup))

    def test_supersession_branching_fails_closed(self):
        # Two profiles superseded by the SAME replacement (reuse/branching) breaks
        # one-to-one replacement history. The bad rows are injected through a table
        # without the strengthened UNIQUE(superseded_by), mirroring a legacy table.
        def setup(con):
            con.execute("INSERT INTO settlement_endpoints(id) VALUES (3)")
            con.executescript(_FK_ONLY_PROFILES_DDL)  # no UNIQUE(superseded_by)
            self._insert(con, [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "superseded", 3, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "superseded", 3, SYN_TIME, SYN_TIME),  # reuses #3
                (3, migrate.SYNTHETIC_TENANT_ID, 3, "active", None, SYN_TIME, SYN_TIME),
            ])
        self._assert_fails_closed(self._v5("branching.sqlite", setup))

    def test_valid_multi_hop_history_reports_already_current(self):
        # 1 -> 2 -> 3 is a legal history: 1 and 2 superseded, 3 active. The
        # strengthened validator must PRESERVE it, not over-reject valid lineage.
        def setup(con):
            con.execute("INSERT INTO settlement_endpoints(id) VALUES (3)")
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            self._insert(con, [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "superseded", 2, SYN_TIME, SYN_TIME),
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "superseded", 3, SYN_TIME, SYN_TIME),
                (3, migrate.SYNTHETIC_TENANT_ID, 3, "active", None, SYN_TIME, SYN_TIME),
            ])
            create_current_repair_tables(con)
        path = self._v5("multi_hop.sqlite", setup)
        self.assertEqual(migrate.migrate(path)["status"], "already_current")

    # -- SEC-P20 blocker 2: named DECLARED constraint rails. Current rows may pass
    # every data check while the table has silently dropped a declared rail, so a
    # later write could violate it. Each fixture carries valid bijective rows and
    # drops exactly one rail.

    def _two_active(self, con):
        self._insert(con, [
            (1, migrate.SYNTHETIC_TENANT_ID, 1, "active", None, SYN_TIME, SYN_TIME),
            (2, migrate.SYNTHETIC_TENANT_ID, 2, "active", None, SYN_TIME, SYN_TIME),
        ])

    def test_missing_unique_endpoint_id_rail_fails_closed(self):
        def setup(con):
            con.executescript(_NO_UNIQUE_ENDPOINT_DDL)  # endpoint_id FK but no UNIQUE
            self._two_active(con)
        self._assert_fails_closed(self._v5("no_unique_endpoint.sqlite", setup))

    def test_missing_lifecycle_check_rail_fails_closed(self):
        def setup(con):
            con.executescript(_NO_LIFECYCLE_CHECK_DDL)  # lifecycle_state has no CHECK
            self._two_active(con)
        self._assert_fails_closed(self._v5("no_lifecycle_check.sqlite", setup))

    def test_missing_self_referential_fk_rail_fails_closed(self):
        def setup(con):
            con.executescript(_NO_SELF_FK_DDL)  # superseded_by has no self-FK
            self._two_active(con)
        self._assert_fails_closed(self._v5("no_self_fk.sqlite", setup))

    def test_missing_not_null_rail_fails_closed(self):
        def setup(con):
            con.executescript(_NULLABLE_COLUMN_DDL)  # created_at dropped NOT NULL
            self._two_active(con)
        self._assert_fails_closed(self._v5("nullable_created_at.sqlite", setup))

    def test_missing_primary_key_rail_fails_closed(self):
        def setup(con):
            con.executescript(_NO_PK_DDL)  # id is not the primary key
            self._two_active(con)
        self._assert_fails_closed(self._v5("no_pk.sqlite", setup))

    def test_missing_unique_superseded_by_rail_fails_closed(self):
        # Otherwise-exact current v5 that drops ONLY the UNIQUE(superseded_by) rail.
        # Valid non-branching rows pass every data check, but a future write could
        # branch replacement history -- so already_current must not be reported.
        def setup(con):
            ddl = migrate.ENDPOINT_PROFILES_DDL.replace(
                "superseded_by INTEGER UNIQUE REFERENCES", "superseded_by INTEGER REFERENCES"
            )
            con.executescript(ddl)
            self._two_active(con)
        self._assert_fails_closed(self._v5("no_unique_superseded.sqlite", setup))

    def test_endpoint_fk_wrong_target_column_fails_closed(self):
        # endpoint_id foreign key points at the wrong target COLUMN
        # (settlement_endpoints.alt, not id). Matching only the source table+column
        # accepts it; the exact FK tuple endpoint_id -> settlement_endpoints(id)
        # must be required.
        def setup(con):
            con.execute("DROP TABLE settlement_endpoints")
            con.execute("CREATE TABLE settlement_endpoints (id INTEGER PRIMARY KEY, alt INTEGER UNIQUE)")
            con.execute("INSERT INTO settlement_endpoints(id, alt) VALUES (1, 101), (2, 102)")
            ddl = migrate.ENDPOINT_PROFILES_DDL.replace(
                "endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(id)",
                "endpoint_id INTEGER NOT NULL UNIQUE REFERENCES settlement_endpoints(alt)",
            )
            con.executescript(ddl)
            self._two_active(con)
        self._assert_fails_closed(self._v5("endpoint_fk_wrong_col.sqlite", setup))

    def test_self_fk_wrong_target_column_fails_closed(self):
        # superseded_by self-foreign-key points at the wrong target COLUMN
        # (endpoint_profiles.endpoint_id, not id); the exact tuple
        # superseded_by -> endpoint_profiles(id) must be required.
        def setup(con):
            ddl = migrate.ENDPOINT_PROFILES_DDL.replace(
                "superseded_by INTEGER UNIQUE REFERENCES endpoint_profiles(id)",
                "superseded_by INTEGER UNIQUE REFERENCES endpoint_profiles(endpoint_id)",
            )
            con.executescript(ddl)
            self._two_active(con)
        self._assert_fails_closed(self._v5("self_fk_wrong_col.sqlite", setup))

    def test_composite_primary_key_fails_closed(self):
        # PK(id, tenant_id): id still has PK ordinal 1 (pk-flag check passes) but the
        # declared identity is not the exact single-column id primary key.
        def setup(con):
            con.executescript(_COMPOSITE_PK_DDL)
            self._two_active(con)
        self._assert_fails_closed(self._v5("composite_pk.sqlite", setup))

    def test_destructive_fk_action_fails_closed(self):
        # endpoint_id FK altered to ON DELETE CASCADE: table/from/to still match, but
        # it is no longer the declared default-action rail.
        def setup(con):
            ddl = migrate.ENDPOINT_PROFILES_DDL.replace(
                "REFERENCES settlement_endpoints(id)",
                "REFERENCES settlement_endpoints(id) ON DELETE CASCADE",
            )
            con.executescript(ddl)
            self._two_active(con)
        self._assert_fails_closed(self._v5("fk_cascade.sqlite", setup))

    def test_relationship_check_removed_fails_closed(self):
        # The lifecycle/superseded_by relationship CHECK is gone while the lifecycle
        # enum CHECK remains; valid rows pass, but a future write could break the
        # superseded_by relationship.
        def setup(con):
            con.executescript(_NO_RELATIONSHIP_CHECK_DDL)
            self._two_active(con)
        self._assert_fails_closed(self._v5("no_rel_check.sqlite", setup))

    def test_relationship_check_weakened_fails_closed(self):
        # The relationship CHECK is kept but self-link protection (superseded_by <> id)
        # is removed; current rows are valid yet a self-superseding write becomes possible.
        def setup(con):
            ddl = migrate.ENDPOINT_PROFILES_DDL.replace(" AND superseded_by <> id", "")
            con.executescript(ddl)
            self._two_active(con)
        self._assert_fails_closed(self._v5("weak_rel_check.sqlite", setup))

    def test_recased_lifecycle_literal_fails_closed(self):
        # Only the quoted lifecycle CHECK literal is re-cased ('draft' -> 'DRAFT').
        # Active rows still insert, but a normal lowercase 'draft' write would now
        # fail the CHECK. A fingerprint that lower-cases quoted string literals would
        # wrongly accept this altered table; the exact declared literal case must be
        # preserved so this fails closed.
        def setup(con):
            ddl = migrate.ENDPOINT_PROFILES_DDL.replace("'draft'", "'DRAFT'")
            con.executescript(ddl)
            self._two_active(con)
        self._assert_fails_closed(self._v5("recased_lifecycle_literal.sqlite", setup))


class EndpointProfilesDdlConstraintTests(unittest.TestCase):
    """The strengthened DDL rejects bad rows at write time: UNIQUE endpoint_id,
    and a lifecycle/superseded_by CHECK."""

    def _fresh(self):
        con = sqlite3.connect(":memory:")
        con.executescript("CREATE TABLE settlement_endpoints(id INTEGER PRIMARY KEY);")
        con.executemany("INSERT INTO settlement_endpoints(id) VALUES (?)", [(1,), (2,)])
        con.executescript(migrate.ENDPOINT_PROFILES_DDL)
        return con

    def _insert(self, con, *, pid, endpoint_id, state, superseded_by=None):
        con.execute(
            "INSERT INTO endpoint_profiles(id, tenant_id, endpoint_id, lifecycle_state, superseded_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (pid, "synthetic-demo", endpoint_id, state, superseded_by, "t", "t"),
        )

    def test_unique_endpoint_id_enforced(self):
        con = self._fresh()
        self.addCleanup(con.close)
        self._insert(con, pid=1, endpoint_id=1, state="active")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(con, pid=2, endpoint_id=1, state="draft")  # duplicate endpoint_id

    def test_draft_or_active_cannot_carry_superseded_by(self):
        con = self._fresh()
        self.addCleanup(con.close)
        for state in ("draft", "active"):
            with self.subTest(state=state):
                with self.assertRaises(sqlite3.IntegrityError):
                    self._insert(con, pid=10, endpoint_id=1, state=state, superseded_by=2)

    def test_superseded_requires_non_self_link(self):
        con = self._fresh()
        self.addCleanup(con.close)
        with self.assertRaises(sqlite3.IntegrityError):  # superseded needs a link
            self._insert(con, pid=1, endpoint_id=1, state="superseded", superseded_by=None)
        with self.assertRaises(sqlite3.IntegrityError):  # link must not be self
            self._insert(con, pid=2, endpoint_id=1, state="superseded", superseded_by=2)

    def test_valid_lifecycle_rows_accepted(self):
        con = self._fresh()
        self.addCleanup(con.close)
        self._insert(con, pid=1, endpoint_id=1, state="active")
        self._insert(con, pid=2, endpoint_id=2, state="superseded", superseded_by=1)  # valid non-self link
        self.assertEqual(con.execute("SELECT COUNT(*) FROM endpoint_profiles").fetchone()[0], 2)

    def test_superseded_by_is_unique_one_to_one_replacement(self):
        # One-to-one replacement history: a replacement supersedes at most one
        # profile, so two superseded profiles cannot share a superseded_by target.
        con = self._fresh()
        self.addCleanup(con.close)
        con.execute("INSERT INTO settlement_endpoints(id) VALUES (3)")
        self._insert(con, pid=3, endpoint_id=3, state="active")  # the replacement head
        self._insert(con, pid=1, endpoint_id=1, state="superseded", superseded_by=3)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(con, pid=2, endpoint_id=2, state="superseded", superseded_by=3)  # reuses #3

    def test_distinct_superseded_by_targets_accepted(self):
        # Distinct replacement targets (a valid multi-hop history) are still allowed.
        con = self._fresh()
        self.addCleanup(con.close)
        con.execute("INSERT INTO settlement_endpoints(id) VALUES (3)")
        self._insert(con, pid=1, endpoint_id=1, state="superseded", superseded_by=2)
        self._insert(con, pid=2, endpoint_id=2, state="superseded", superseded_by=3)
        self._insert(con, pid=3, endpoint_id=3, state="active")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM endpoint_profiles").fetchone()[0], 3)


class RepairSchemaRailValidationTests(TempDbTestCase):
    """SEC-P30 (v6): valid CURRENT markers + ledger + a well-formed endpoint_profiles
    registry are not enough. A DB claiming CURRENT must also carry the exact declared
    profile_decisions, repair_tasks, and repair_events shapes and the exact one-open
    partial UNIQUE index. Each fixture keeps every OTHER rail intact and weakens or
    drops exactly one declared rail; current-schema validation must fail closed and
    preserve state. Mirrors the endpoint_profiles rail tests.
    """

    def _current_v6(self, name, *, decisions_ddl=None, repair_ddl=None,
                    events_ddl=None, open_index_ddl=None, mutate=None):
        """A DB stamped CURRENT with the full registry, substituting any weakened DDL."""
        path = self.db(name)
        build_db(
            path,
            user_version=migrate.CURRENT_SCHEMA_VERSION,
            seed_meta={"schema_version": str(migrate.CURRENT_SCHEMA_VERSION)},
            ledger_rows=GOOD_LEDGER,
        )
        con = sqlite3.connect(path)  # foreign_keys OFF by default -> inject freely
        try:
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            migrate.backfill_active_profiles(con, migrate.SYNTHETIC_TENANT_ID, SYN_TIME)
            con.executescript(decisions_ddl or migrate.PROFILE_DECISIONS_DDL)
            con.executescript(repair_ddl or migrate.REPAIR_TASKS_DDL)
            con.executescript(open_index_ddl or migrate.REPAIR_TASKS_OPEN_INDEX_DDL)
            con.executescript(events_ddl or migrate.REPAIR_EVENTS_DDL)
            if mutate is not None:
                mutate(con)
            con.commit()
        finally:
            con.close()
        return path

    def _assert_fails_closed(self, path):
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.migrate(path)
        self.assertEqual(cm.exception.code, "inconsistent_version")
        self.assertEqual(read_markers(path), (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)))

    # -- GREEN anchor: the exact current v6 registry reports already_current -------

    def test_exact_current_v6_reports_already_current(self):
        self.assertEqual(migrate.migrate(self._current_v6("exact_v6.sqlite"))["status"], "already_current")

    # -- profile_decisions rails --------------------------------------------------

    def test_decisions_missing_table_fails_closed(self):
        self._assert_fails_closed(self._current_v6(
            "dec_missing.sqlite", decisions_ddl="CREATE TABLE _unused_decisions (id INTEGER PRIMARY KEY);"
        ))

    def test_decisions_rail_drops_fail_closed(self):
        base = migrate.PROFILE_DECISIONS_DDL
        variants = {
            "drop_unique_profile_version": base.replace("    UNIQUE(profile_id, version),\n", ""),
            "drop_unique_previous_decision": base.replace(
                "previous_decision_id INTEGER UNIQUE REFERENCES", "previous_decision_id INTEGER REFERENCES"
            ),
            "drop_origin_enum_check": base.replace(
                "origin TEXT NOT NULL CHECK(origin IN ('baseline','revalidation'))", "origin TEXT NOT NULL"
            ),
            "recase_origin_literal": base.replace("'baseline'", "'BASELINE'"),
            "drop_notnull_verdict": base.replace("verdict TEXT NOT NULL", "verdict TEXT"),
            "drop_profile_fk": base.replace(
                "profile_id INTEGER NOT NULL REFERENCES endpoint_profiles(id)", "profile_id INTEGER NOT NULL"
            ),
            "drop_version_relationship_check": base.replace(
                "    CHECK(\n"
                "        (version = 1 AND origin = 'baseline' AND previous_decision_id IS NULL)\n"
                "        OR (version > 1 AND origin = 'revalidation' AND previous_decision_id IS NOT NULL AND previous_decision_id <> id)\n"
                "    )\n",
                "    CHECK(version >= 1)\n",
            ),
            "weaken_self_link_guard": base.replace(" AND previous_decision_id <> id", ""),
        }
        for label, ddl in variants.items():
            with self.subTest(rail=label):
                self.assertNotEqual(ddl, base, f"{label} did not alter the DDL")
                self._assert_fails_closed(self._current_v6(f"dec_{label}.sqlite", decisions_ddl=ddl))

    # -- repair_tasks rails -------------------------------------------------------

    def test_repair_rail_drops_fail_closed(self):
        base = migrate.REPAIR_TASKS_DDL
        variants = {
            "drop_unique_resolved_decision": base.replace(
                "resolved_decision_id INTEGER UNIQUE REFERENCES", "resolved_decision_id INTEGER REFERENCES"
            ),
            "drop_state_enum_check": base.replace(
                "state TEXT NOT NULL CHECK(state IN ('open','evidence_refreshed','resolved'))", "state TEXT NOT NULL"
            ),
            "recase_state_literal": base.replace("'resolved'", "'RESOLVED'"),
            "drop_opened_decision_fk": base.replace(
                "opened_decision_id INTEGER NOT NULL REFERENCES profile_decisions(id)",
                "opened_decision_id INTEGER NOT NULL",
            ),
            "drop_notnull_actor": base.replace("actor TEXT NOT NULL", "actor TEXT"),
        }
        for label, ddl in variants.items():
            with self.subTest(rail=label):
                self.assertNotEqual(ddl, base, f"{label} did not alter the DDL")
                self._assert_fails_closed(self._current_v6(f"rt_{label}.sqlite", repair_ddl=ddl))

    def test_repair_state_relationship_check_drop_fails_closed(self):
        # Drop the whole state/field relationship CHECK, keeping the enum CHECK.
        base = migrate.REPAIR_TASKS_DDL
        # Excise the trailing comma too, so the weakened table is valid SQL (a bare
        # comma before the closing paren would be a syntax error, not a weakened rail).
        start = base.index(",\n    CHECK(\n        (state = 'open'")
        end = base.index("    )\n);")
        ddl = base[:start] + base[end + len("    )\n"):]
        self.assertNotIn("refreshed_authority_status IS NULL", ddl)
        self._assert_fails_closed(self._current_v6("rt_no_rel_check.sqlite", repair_ddl=ddl))

    # -- the one-open partial UNIQUE index predicate ------------------------------

    def test_partial_index_wrong_predicate_fails_closed(self):
        # A partial UNIQUE index on profile_id but with a DIFFERENT predicate must
        # fail closed: matching only "some partial unique index on profile_id" would
        # wrongly accept a rail that does not enforce one-open-per-profile.
        wrong = (
            "CREATE UNIQUE INDEX IF NOT EXISTS repair_tasks_one_open_per_profile "
            "ON repair_tasks(profile_id) WHERE state = 'open';"
        )
        self._assert_fails_closed(self._current_v6("idx_wrong_pred.sqlite", open_index_ddl=wrong))

    def test_partial_index_dropped_fails_closed(self):
        self._assert_fails_closed(self._current_v6("idx_missing.sqlite", open_index_ddl="SELECT 1;"))

    def test_full_unique_index_instead_of_partial_fails_closed(self):
        # A full (non-partial) UNIQUE index on profile_id forbids ALL re-repair, not
        # merely a second LIVE one; it is not the declared one-open rail.
        full = (
            "CREATE UNIQUE INDEX IF NOT EXISTS repair_tasks_one_open_per_profile "
            "ON repair_tasks(profile_id);"
        )
        self._assert_fails_closed(self._current_v6("idx_full.sqlite", open_index_ddl=full))

    # -- repair_events rails ------------------------------------------------------

    def test_events_missing_table_fails_closed(self):
        self._assert_fails_closed(self._current_v6(
            "ev_missing.sqlite", events_ddl="CREATE TABLE _unused_events (id INTEGER PRIMARY KEY);"
        ))

    def test_events_rail_drops_fail_closed(self):
        base = migrate.REPAIR_EVENTS_DDL
        variants = {
            "drop_unique_task_sequence": base.replace("    UNIQUE(task_id, sequence),\n", ""),
            "drop_unique_task_event": base.replace("    UNIQUE(task_id, event_type),\n", ""),
            "drop_event_enum_check": base.replace(
                "event_type TEXT NOT NULL CHECK(event_type IN ('repair_opened','evidence_refreshed','revalidated'))",
                "event_type TEXT NOT NULL",
            ),
            "recase_event_literal": base.replace("'repair_opened'", "'REPAIR_OPENED'"),
            "drop_task_fk": base.replace(
                "task_id INTEGER NOT NULL REFERENCES repair_tasks(id)", "task_id INTEGER NOT NULL"
            ),
            "drop_notnull_sequence": base.replace("sequence INTEGER NOT NULL", "sequence INTEGER"),
        }
        for label, ddl in variants.items():
            with self.subTest(rail=label):
                self.assertNotEqual(ddl, base, f"{label} did not alter the DDL")
                self._assert_fails_closed(self._current_v6(f"ev_{label}.sqlite", events_ddl=ddl))


class RepairDecisionsV6MigrationTests(TempDbTestCase):
    """SEC-P30: the real 5 -> 6 repair/decision migration against a TRUE v5 fixture.

    A genuine v5 database carries the foundation ledger and the endpoint_profiles
    registry (with its backfilled active profiles) but NONE of the v6 tables. The
    5 -> 6 step must additively create profile_decisions, repair_tasks, repair_events
    and the exact one-open partial UNIQUE index, preserve every existing profile /
    operator-action / audit row byte-for-byte, be idempotent on a second run, and
    roll the WHOLE step back atomically on an induced mid-step failure -- leaving the
    v5 markers, data, and tables exactly as they were.
    """

    def _seed_full(self, path: Path) -> None:
        with mock.patch.object(seed, "DB_PATH", path):
            seed.seed()

    def _downgrade_to_v5(self, path: Path) -> None:
        """Turn a fresh CURRENT (v6) seed into a genuine v5 state.

        The endpoint_profiles registry is version 5, so it (and its backfilled
        rows) stays; only the version-6 decision/repair/event tables are dropped
        and only the version-6 ledger row is stripped, with the markers moved back
        to 5. Deleting rows with ``version > 5`` (not merely ``== 6``) keeps the
        strip future-proof.
        """
        con = sqlite3.connect(path)
        try:
            con.execute("DROP TABLE IF EXISTS repair_events")
            con.execute("DROP TABLE IF EXISTS repair_tasks")
            con.execute("DROP TABLE IF EXISTS profile_decisions")
            con.execute("DELETE FROM schema_migrations WHERE version > 5")
            con.execute("UPDATE seed_meta SET value='5' WHERE key='schema_version'")
            con.execute("PRAGMA user_version = 5")
            con.commit()
        finally:
            con.close()

    def _add_offbaseline_rows(self, path: Path) -> None:
        """A synthetic operator action and audit event a fresh seed never writes,
        so their preservation across 5 -> 6 can only be persistent local state."""
        con = sqlite3.connect(path)
        try:
            con.execute(
                "INSERT INTO operator_actions(scenario_id, action_type, actor, status, detail, created_at)"
                " VALUES (1,'open_repair_task','ops_analyst','recorded','SYN-P30-preserve','2026-06-05T09:00:00Z')"
            )
            con.execute(
                "INSERT INTO audit_events(scenario_id, display_order, event_type, title, detail, created_at)"
                " VALUES (1,99,'action','Repair task opened','SYN-P30-audit','2026-06-05T09:00:00Z')"
            )
            con.commit()
        finally:
            con.close()

    def _true_v5(self, name: str) -> Path:
        """A genuine v5 DB with off-baseline preserved rows and no v6 tables."""
        path = self.db(name)
        self._seed_full(path)
        self._add_offbaseline_rows(path)
        self._downgrade_to_v5(path)
        # Sanity: a real v5 state resolves to 5 and carries none of the v6 tables.
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        self.assertEqual(migrate.detect_version(con), 5)
        for table in ("profile_decisions", "repair_tasks", "repair_events"):
            self.assertFalse(table_exists(path, table), f"{table} must be absent at true v5")
        return path

    @staticmethod
    def _table_ddl(path: Path, name: str) -> str:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()
            return row[0] if row and row[0] else ""
        finally:
            con.close()

    def test_v5_to_v6_creates_repair_tables_and_exact_partial_index(self) -> None:
        path = self._true_v5("v5_create.sqlite")

        result = migrate.migrate(path)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(result["from"], 5)
        self.assertEqual(result["to"], migrate.CURRENT_SCHEMA_VERSION)
        self.assertEqual(result["applied"], [migrate.REPAIR_DECISIONS_MIGRATION_NAME])

        # The three v6 tables now exist and each fingerprints to its exact declared shape.
        for table, canonical in (
            ("profile_decisions", migrate._CANONICAL_PROFILE_DECISIONS_DDL),
            ("repair_tasks", migrate._CANONICAL_REPAIR_TASKS_DDL),
            ("repair_events", migrate._CANONICAL_REPAIR_EVENTS_DDL),
        ):
            self.assertTrue(table_exists(path, table), f"{table} must be created by 5->6")
            self.assertEqual(migrate._canonical_table_ddl(self._table_ddl(path, table)), canonical)
        # The one-open partial UNIQUE index carries its exact declared predicate.
        self.assertEqual(
            migrate._canonical_table_ddl(self._table_ddl(path, "repair_tasks_one_open_per_profile")),
            migrate._CANONICAL_REPAIR_OPEN_INDEX_DDL,
        )
        # Born empty (a purely additive step writes no repair rows).
        for table in ("profile_decisions", "repair_tasks", "repair_events"):
            self.assertEqual(len(read_rows(path, table, order_by="rowid")), 0)
        self.assertEqual(read_markers(path), (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)))

    def test_v5_to_v6_preserves_profiles_actions_and_audit(self) -> None:
        path = self._true_v5("v5_preserve.sqlite")
        profiles_before = read_rows(path, "endpoint_profiles", order_by="id")
        business_before = business_snapshot(path)
        self.assertEqual(len(read_rows(path, "operator_actions")), 1, "off-baseline operator action present")

        result = migrate.migrate(path)
        self.assertEqual(result["status"], "migrated")

        # Endpoint profiles and every business table are byte-for-byte identical.
        self.assertEqual(read_rows(path, "endpoint_profiles", order_by="id"), profiles_before)
        self.assertEqual(business_snapshot(path), business_before)

        # Structural integrity intact after the additive step.
        con = sqlite3.connect(path)
        self.addCleanup(con.close)
        con.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_v5_to_v6_is_idempotent(self) -> None:
        path = self._true_v5("v5_idem.sqlite")
        first = migrate.migrate(path)
        second = migrate.migrate(path)
        self.assertEqual(first["status"], "migrated")
        self.assertEqual(second["status"], "already_current")
        self.assertEqual(second["applied"], [])
        # No duplicate ledger rows and the repair tables stay empty on a second pass.
        self.assertEqual(len(read_rows(path, "schema_migrations", order_by="version")), len(migrate.current_ledger()))
        self.assertEqual(len(read_rows(path, "repair_events", order_by="rowid")), 0)

    def test_v5_to_v6_rolls_back_whole_step_on_induced_failure(self) -> None:
        # A 5 -> 6 step that creates ALL of the v6 tables and index and then fails
        # mid-step must roll the WHOLE step back atomically: none of the three
        # tables survive and the markers stay at v5 with data preserved.
        path = self._true_v5("v5_rollback.sqlite")
        profiles_before = read_rows(path, "endpoint_profiles", order_by="id")
        business_before = business_snapshot(path)

        def boom(con: sqlite3.Connection) -> None:
            migrate._migrate_5_to_6(con)  # create every v6 table + the partial index
            con.execute(
                "INSERT INTO profile_decisions(tenant_id, profile_id, version, previous_decision_id, origin,"
                " verdict, token_class, fiat_class, token_text, fiat_text, repair_text,"
                " evidence_authority_status, evidence_allowlist_status, evidence_payload_status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (migrate.SYNTHETIC_TENANT_ID, 1, 1, None, "baseline", "V", "warned", "warned",
                 "t", "t", "t", "current", "current", "complete", SYN_TIME),
            )
            raise RuntimeError("induced 5->6 mid-step failure")

        failing = [(5, migrate.CURRENT_SCHEMA_VERSION, "boom_repair", boom)]
        with mock.patch.object(migrate, "MIGRATIONS", failing):
            with self.assertRaises(migrate.MigrationError) as cm:
                migrate.migrate(path)
        self.assertEqual(cm.exception.code, "migration_failed")

        # The whole step rolled back: no v6 table survives, markers stay v5.
        for table in ("profile_decisions", "repair_tasks", "repair_events"):
            self.assertFalse(table_exists(path, table), f"{table} creation must roll back")
        self.assertFalse(
            table_exists(path, "repair_tasks_one_open_per_profile"),
            "the partial index must roll back with its table",
        )
        self.assertEqual(read_markers(path), (5, "5"), "version markers must stay at v5")
        # Existing v5 data preserved byte-for-byte (profiles and every business table).
        self.assertEqual(read_rows(path, "endpoint_profiles", order_by="id"), profiles_before)
        self.assertEqual(business_snapshot(path), business_before)


# Ordered synthetic step timestamps for the repair chain (T1 < T2 < ... < T6). The
# first three drive a single cycle; T4..T6 extend it to a second (repeated) cycle.
_T1 = "2026-06-05T09:00:00Z"
_T2 = "2026-06-05T09:00:01Z"
_T3 = "2026-06-05T09:00:02Z"
_T4 = "2026-06-05T09:00:03Z"
_T5 = "2026-06-05T09:00:04Z"
_T6 = "2026-06-05T09:00:05Z"

# The canonical synthetic fallback contract (the only supported values). A repair
# chain fixture uses it so evaluator recomputation over the chain's evidence
# matches the stored decision, exactly as the running app records it.
_CANON_FALLBACK = {
    "fallback_rail": "Fiat SSI route",
    "fallback_currency": "EUR",
    "fallback_account_mask": "DE•• •••• •••• 4400",
    "fallback_intermediary_bic": "INTERDEFFXXX",
}
# The intrinsic constituent evidence the repair-chain fixture gives each backfilled
# endpoint: a stale allowlist makes the intrinsic verdict BLOCKED, so a baseline
# decision recorded from it is a genuine failing decision the repair supersedes.
_CHAIN_INTRINSIC = {"authority_status": "current", "allowlist_status": "stale", "endpoint_payload_status": "complete"}


def _eval_decision(*, authority, allowlist, payload):
    """The evaluator's full decision over the given graded evidence plus the
    canonical synthetic structure/fallback -- exactly what the app persists."""
    import evaluator
    return evaluator.evaluate({
        "institution_present": True,
        "institution_reachable": True,
        "legal_entity_present": True,
        "authority_status": authority,
        "allowlist_status": allowlist,
        "payload_status": payload,
        **_CANON_FALLBACK,
    })["decision"]


def enrich_chain_constituents(con: sqlite3.Connection) -> None:
    """Give the minimal fixture endpoints real constituent evidence + a legal entity.

    The base fixture's settlement_endpoints has only an ``id`` column; the v6
    row-integrity validator recomputes each stored decision from the profile's
    intrinsic constituent fields, so a repair-chain fixture must carry those fields
    (a legal entity's authority status and the endpoint's allowlist / payload /
    fallback) for every backfilled endpoint.
    """
    con.executescript(
        "CREATE TABLE legal_entities (id INTEGER PRIMARY KEY, authority_status TEXT NOT NULL);\n"
        "ALTER TABLE settlement_endpoints ADD COLUMN legal_entity_id INTEGER;\n"
        "ALTER TABLE settlement_endpoints ADD COLUMN allowlist_status TEXT;\n"
        "ALTER TABLE settlement_endpoints ADD COLUMN endpoint_payload_status TEXT;\n"
        "ALTER TABLE settlement_endpoints ADD COLUMN fallback_rail TEXT;\n"
        "ALTER TABLE settlement_endpoints ADD COLUMN fallback_currency TEXT;\n"
        "ALTER TABLE settlement_endpoints ADD COLUMN fallback_account_mask TEXT;\n"
        "ALTER TABLE settlement_endpoints ADD COLUMN fallback_intermediary_bic TEXT;\n"
    )
    for (endpoint_id,) in con.execute("SELECT id FROM settlement_endpoints ORDER BY id").fetchall():
        con.execute(
            "INSERT INTO legal_entities(id, authority_status) VALUES (?, ?)",
            (endpoint_id, _CHAIN_INTRINSIC["authority_status"]),
        )
        con.execute(
            "UPDATE settlement_endpoints SET legal_entity_id=?, allowlist_status=?, endpoint_payload_status=?,"
            " fallback_rail=?, fallback_currency=?, fallback_account_mask=?, fallback_intermediary_bic=? WHERE id=?",
            (
                endpoint_id, _CHAIN_INTRINSIC["allowlist_status"], _CHAIN_INTRINSIC["endpoint_payload_status"],
                _CANON_FALLBACK["fallback_rail"], _CANON_FALLBACK["fallback_currency"],
                _CANON_FALLBACK["fallback_account_mask"], _CANON_FALLBACK["fallback_intermediary_bic"], endpoint_id,
            ),
        )


class RepairRowIntegrityValidationTests(TempDbTestCase):
    """SEC-P30 (v6): an exact-shape v6 registry is not enough -- the stored
    decision / repair / event ROWS must obey the cross-row invariants no per-row
    SQLite constraint can prove. A valid baseline -> revalidation decision chain, a
    resolved task, and its three-event trail are the green anchor; each focused
    corruption injects exactly one cross-row defect that must fail closed with the
    stable ``inconsistent_version`` code while preserving the version markers. The
    exact-schema rails are never weakened (see RepairSchemaRailValidationTests).
    """

    def _current_v6_with_chain(self, name, *, corrupt=None) -> Path:
        """A DB stamped CURRENT with the full registry and one valid repair chain.

        foreign_keys is OFF for the fixture connection, so a corruption callback can
        inject rows the running app never would (e.g. a decision on an absent
        profile) to exercise the validator directly.
        """
        path = self.db(name)
        build_db(
            path,
            user_version=migrate.CURRENT_SCHEMA_VERSION,
            seed_meta={"schema_version": str(migrate.CURRENT_SCHEMA_VERSION)},
            ledger_rows=GOOD_LEDGER,
        )
        con = sqlite3.connect(path)
        try:
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            migrate.backfill_active_profiles(con, migrate.SYNTHETIC_TENANT_ID, SYN_TIME)
            enrich_chain_constituents(con)
            create_current_repair_tables(con)
            self._insert_valid_chain(con)
            if corrupt is not None:
                corrupt(con)
            con.commit()
        finally:
            con.close()
        return path

    @staticmethod
    def _insert_decision(con, *, did, profile_id, version, previous, origin, tenant=migrate.SYNTHETIC_TENANT_ID,
                         authority="current", allowlist="current", payload="complete", created_at=_T1,
                         verdict="V", decision=None):
        """Insert a decision row. By default it carries a placeholder verdict/text
        (fine for corruption cases that fail an earlier structural rail); pass
        ``decision`` (a full evaluator decision dict) for a row that must survive the
        deterministic-decision check on a real profile chain."""
        if decision is None:
            decision = {"verdict": verdict, "token_class": "warned", "fiat_class": "warned",
                        "token_text": "t", "fiat_text": "t", "repair_text": "t"}
        con.execute(
            "INSERT INTO profile_decisions(id, tenant_id, profile_id, version, previous_decision_id, origin,"
            " verdict, token_class, fiat_class, token_text, fiat_text, repair_text,"
            " evidence_authority_status, evidence_allowlist_status, evidence_payload_status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, tenant, profile_id, version, previous, origin,
             decision["verdict"], decision["token_class"], decision["fiat_class"],
             decision["token_text"], decision["fiat_text"], decision["repair_text"],
             authority, allowlist, payload, created_at),
        )

    def _insert_valid_chain(self, con) -> None:
        """profile 1: v1 baseline (intrinsic BLOCKED) -> v2 revalidation (refreshed
        ALLOW), a resolved repair task, and the ordered three-event trail. Both
        decisions are evaluator-consistent: the baseline snapshots the profile's
        intrinsic constituents and the revalidation the refreshed evidence."""
        baseline = _eval_decision(authority="current", allowlist="stale", payload="complete")
        revalidation = _eval_decision(authority="current", allowlist="current", payload="complete")
        self._insert_decision(con, did=1, profile_id=1, version=1, previous=None, origin="baseline",
                              authority="current", allowlist="stale", payload="complete",
                              created_at=_T1, decision=baseline)
        self._insert_decision(con, did=2, profile_id=1, version=2, previous=1, origin="revalidation",
                              authority="current", allowlist="current", payload="complete",
                              created_at=_T3, decision=revalidation)
        con.execute(
            "INSERT INTO repair_tasks(id, tenant_id, profile_id, actor, state, opened_decision_id, resolved_decision_id,"
            " refreshed_authority_status, refreshed_allowlist_status, refreshed_payload_status,"
            " created_at, evidence_refreshed_at, resolved_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, migrate.SYNTHETIC_TENANT_ID, 1, "ops_analyst", "resolved", 1, 2,
             "current", "current", "complete", _T1, _T2, _T3),
        )
        con.executemany(
            "INSERT INTO repair_events(id, tenant_id, profile_id, task_id, sequence, event_type, actor, decision_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, 1, 1, "repair_opened", "ops_analyst", 1, _T1),
                (2, migrate.SYNTHETIC_TENANT_ID, 1, 1, 2, "evidence_refreshed", "ops_analyst", None, _T2),
                (3, migrate.SYNTHETIC_TENANT_ID, 1, 1, 3, "revalidated", "ops_analyst", 2, _T3),
            ],
        )

    def _current_v6_with_repeated_cycle(self, name, *, corrupt=None) -> Path:
        """A DB stamped CURRENT with the full registry and a legal REPEATED repair
        history on profile 1: v1 baseline (BLOCKED) resolved to v2 (a still-failing
        revalidation HOLD), then a SECOND cycle opening that v2 and resolving to v3
        (ALLOW). Two resolved tasks, each with its ordered three-event trail -- the
        'repeated HOLD -> next-repair cycle' legal state. A corruption callback can
        inject exactly one cross-cycle defect (foreign keys are OFF here)."""
        path = self.db(name)
        build_db(
            path,
            user_version=migrate.CURRENT_SCHEMA_VERSION,
            seed_meta={"schema_version": str(migrate.CURRENT_SCHEMA_VERSION)},
            ledger_rows=GOOD_LEDGER,
        )
        con = sqlite3.connect(path)
        try:
            con.executescript(migrate.ENDPOINT_PROFILES_DDL)
            migrate.backfill_active_profiles(con, migrate.SYNTHETIC_TENANT_ID, SYN_TIME)
            enrich_chain_constituents(con)
            create_current_repair_tables(con)
            self._insert_repeated_cycle(con)
            if corrupt is not None:
                corrupt(con)
            con.commit()
        finally:
            con.close()
        return path

    def _insert_repeated_cycle(self, con) -> None:
        """profile 1: v1 baseline (intrinsic BLOCKED) -> T1 resolves to v2
        (revalidation AUTHORITY_HOLD, still failing) -> T2 opens that v2 and resolves
        to v3 (revalidation ALLOW). Every decision is evaluator-consistent, every
        task opens a FAILING decision, and each task.created_at is >= its opened
        decision.created_at (the v1 open is atomic with the baseline)."""
        baseline = _eval_decision(authority="current", allowlist="stale", payload="complete")      # BLOCKED
        reval_hold = _eval_decision(authority="expired", allowlist="current", payload="complete")   # AUTHORITY_HOLD
        reval_allow = _eval_decision(authority="current", allowlist="current", payload="complete")  # ALLOW
        self._insert_decision(con, did=1, profile_id=1, version=1, previous=None, origin="baseline",
                              authority="current", allowlist="stale", payload="complete",
                              created_at=_T1, decision=baseline)
        self._insert_decision(con, did=2, profile_id=1, version=2, previous=1, origin="revalidation",
                              authority="expired", allowlist="current", payload="complete",
                              created_at=_T3, decision=reval_hold)
        self._insert_decision(con, did=3, profile_id=1, version=3, previous=2, origin="revalidation",
                              authority="current", allowlist="current", payload="complete",
                              created_at=_T6, decision=reval_allow)
        con.executemany(
            "INSERT INTO repair_tasks(id, tenant_id, profile_id, actor, state, opened_decision_id, resolved_decision_id,"
            " refreshed_authority_status, refreshed_allowlist_status, refreshed_payload_status,"
            " created_at, evidence_refreshed_at, resolved_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, "ops_analyst", "resolved", 1, 2,
                 "expired", "current", "complete", _T1, _T2, _T3),
                (2, migrate.SYNTHETIC_TENANT_ID, 1, "ops_analyst", "resolved", 2, 3,
                 "current", "current", "complete", _T4, _T5, _T6),
            ],
        )
        con.executemany(
            "INSERT INTO repair_events(id, tenant_id, profile_id, task_id, sequence, event_type, actor, decision_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (1, migrate.SYNTHETIC_TENANT_ID, 1, 1, 1, "repair_opened", "ops_analyst", 1, _T1),
                (2, migrate.SYNTHETIC_TENANT_ID, 1, 1, 2, "evidence_refreshed", "ops_analyst", None, _T2),
                (3, migrate.SYNTHETIC_TENANT_ID, 1, 1, 3, "revalidated", "ops_analyst", 2, _T3),
                (4, migrate.SYNTHETIC_TENANT_ID, 1, 2, 1, "repair_opened", "ops_analyst", 2, _T4),
                (5, migrate.SYNTHETIC_TENANT_ID, 1, 2, 2, "evidence_refreshed", "ops_analyst", None, _T5),
                (6, migrate.SYNTHETIC_TENANT_ID, 1, 2, 3, "revalidated", "ops_analyst", 3, _T6),
            ],
        )

    def _assert_fails_closed(self, path):
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.migrate(path)
        self.assertEqual(cm.exception.code, "inconsistent_version")
        self.assertEqual(read_markers(path), (migrate.CURRENT_SCHEMA_VERSION, str(migrate.CURRENT_SCHEMA_VERSION)))

    # -- GREEN anchor: the valid chain reports already_current --------------------

    def test_valid_repair_chain_reports_already_current(self):
        path = self._current_v6_with_chain("rows_valid.sqlite")
        self.assertEqual(migrate.migrate(path)["status"], "already_current")

    # -- decision row corruptions -------------------------------------------------

    def test_dangling_decision_profile_fails_closed(self):
        # A self-consistent baseline decision whose profile_id is absent from
        # endpoint_profiles: the per-row FK is not retroactively enforced on stored
        # rows, so only a cross-row profile-resolution check catches it.
        def corrupt(con):
            self._insert_decision(con, did=3, profile_id=9999, version=1, previous=None, origin="baseline")
        self._assert_fails_closed(self._current_v6_with_chain("rows_dangling_decision.sqlite", corrupt=corrupt))

    def test_foreign_tenant_decision_fails_closed(self):
        def corrupt(con):
            self._insert_decision(con, did=3, profile_id=2, version=1, previous=None, origin="baseline",
                                  tenant="attacker-tenant")
        self._assert_fails_closed(self._current_v6_with_chain("rows_foreign_tenant.sqlite", corrupt=corrupt))

    def test_noncontiguous_decision_versions_fail_closed(self):
        # A version gap (1, 2, 4) breaks the contiguous 1..N lineage a valid
        # baseline-then-revalidation chain must form.
        def corrupt(con):
            self._insert_decision(con, did=3, profile_id=1, version=4, previous=2, origin="revalidation",
                                  created_at=_T3)
        self._assert_fails_closed(self._current_v6_with_chain("rows_noncontiguous.sqlite", corrupt=corrupt))

    # -- reverse decision provenance: every decision owned by a repair task -------

    def test_orphan_decision_chain_without_owning_tasks_fails_closed(self):
        # An exact-current, evaluator-consistent v1 baseline -> v2 revalidation chain
        # whose owning repair tasks and events have been removed (foreign keys off in
        # the fixture connection), leaving the decisions orphaned. The chain stays
        # internally consistent and reads would still promote its latest decision,
        # but no task opened the baseline and no task resolved the revalidation, so
        # the required action/event/decision provenance is absent. The forward
        # task -> decision checks cannot see this; only reverse decision provenance
        # (every baseline opened, every revalidation resolved) rejects it.
        def corrupt(con):
            con.execute("DELETE FROM repair_events")
            con.execute("DELETE FROM repair_tasks")
        self._assert_fails_closed(self._current_v6_with_chain("rows_orphan_chain.sqlite", corrupt=corrupt))

    # -- hold-only provenance: a repair may only open a FAILING decision ----------

    def test_task_opens_approved_decision_fails_closed(self):
        # An otherwise-coherent LIVE repair whose opened decision reads as APPROVED.
        # open_repair_task returns nothing_to_repair for an approved latest decision,
        # so a persisted task opening one has no repair basis: it must fail closed.
        # profile 2 (a bare backfilled active profile) is made intrinsically APPROVED
        # and given an open task on an evaluator-consistent APPROVED v1 baseline, so
        # every other invariant holds and only the hold-only rule rejects it.
        def corrupt(con):
            con.execute("UPDATE settlement_endpoints SET allowlist_status='current' WHERE id=2")
            approved = _eval_decision(authority="current", allowlist="current", payload="complete")
            self._insert_decision(con, did=3, profile_id=2, version=1, previous=None, origin="baseline",
                                  authority="current", allowlist="current", payload="complete",
                                  created_at=_T1, decision=approved)
            con.execute(
                "INSERT INTO repair_tasks(id, tenant_id, profile_id, actor, state, opened_decision_id, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "ops_analyst", "open", 3, _T1),
            )
            con.execute(
                "INSERT INTO repair_events(id, tenant_id, profile_id, task_id, sequence, event_type, actor, decision_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (4, migrate.SYNTHETIC_TENANT_ID, 2, 2, 1, "repair_opened", "ops_analyst", 3, _T1),
            )
        self._assert_fails_closed(self._current_v6_with_chain("rows_open_approved.sqlite", corrupt=corrupt))

    # -- opened-decision chronology + the legal repeated-cycle history -------------

    def test_valid_repeated_cycle_reports_already_current(self):
        # The 'repeated HOLD -> next-repair cycle' history is legal and must be
        # preserved: v1 -> v2 (HOLD) -> v3 (ALLOW) across two resolved tasks.
        path = self._current_v6_with_repeated_cycle("rows_repeated_cycle.sqlite")
        self.assertEqual(migrate.migrate(path)["status"], "already_current")

    def test_later_task_predates_opened_revalidation_fails_closed(self):
        # The second cycle's task opens the v2 revalidation decision (created at _T3)
        # but claims a created_at of _T2 -- before that decision existed. Its own step
        # timeline stays monotonic and its events stay step-aligned, so ONLY the
        # opened-decision chronology invariant (task.created_at >= the opened
        # decision's created_at) can reject it.
        def corrupt(con):
            con.execute("UPDATE repair_tasks SET created_at=? WHERE id=2", (_T2,))
            con.execute("UPDATE repair_events SET created_at=? WHERE task_id=2 AND sequence=1", (_T2,))
        self._assert_fails_closed(
            self._current_v6_with_repeated_cycle("rows_task_predates_reval.sqlite", corrupt=corrupt)
        )

    def test_live_task_retargeted_to_non_latest_decision_fails_closed(self):
        # A third, LIVE (open) task whose opened decision AND its event are retargeted
        # to the already-superseded v2 (a non-latest older decision) rather than the
        # latest v3. v2 is already opened by the second cycle's task, so reverse
        # decision provenance (a decision opened by more than one task) rejects it.
        # Complements the orphan-chain regression (a decision opened by NO task).
        def corrupt(con):
            con.execute(
                "INSERT INTO repair_tasks(id, tenant_id, profile_id, actor, state, opened_decision_id, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (3, migrate.SYNTHETIC_TENANT_ID, 1, "ops_analyst", "open", 2, _T6),
            )
            con.execute(
                "INSERT INTO repair_events(id, tenant_id, profile_id, task_id, sequence, event_type, actor, decision_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (7, migrate.SYNTHETIC_TENANT_ID, 1, 3, 1, "repair_opened", "ops_analyst", 2, _T6),
            )
        self._assert_fails_closed(
            self._current_v6_with_repeated_cycle("rows_retarget_older.sqlite", corrupt=corrupt)
        )

    # -- repair task row corruptions ----------------------------------------------

    def test_mis_scoped_task_decision_fails_closed(self):
        # A task on profile 2 whose opened decision belongs to profile 1.
        def corrupt(con):
            con.execute(
                "INSERT INTO repair_tasks(id, tenant_id, profile_id, actor, state, opened_decision_id, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "ops_analyst", "open", 1, _T1),
            )
        self._assert_fails_closed(self._current_v6_with_chain("rows_mis_scoped_task.sqlite", corrupt=corrupt))

    def test_dangling_task_profile_fails_closed(self):
        # A task whose profile_id is absent from endpoint_profiles (its opened
        # decision points at a real profile-1 decision): the task-side of the
        # profile-resolution check must reject it.
        def corrupt(con):
            con.execute(
                "INSERT INTO repair_tasks(id, tenant_id, profile_id, actor, state, opened_decision_id, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (2, migrate.SYNTHETIC_TENANT_ID, 9999, "ops_analyst", "open", 1, _T1),
            )
        self._assert_fails_closed(self._current_v6_with_chain("rows_dangling_task.sqlite", corrupt=corrupt))

    def test_task_timestamp_inversion_fails_closed(self):
        # evidence_refreshed_at precedes created_at: non-monotonic step timeline.
        def corrupt(con):
            con.execute(
                "UPDATE repair_tasks SET evidence_refreshed_at='2026-06-05T08:59:00Z' WHERE id=1"
            )
        self._assert_fails_closed(self._current_v6_with_chain("rows_ts_inversion.sqlite", corrupt=corrupt))

    # -- repair event row corruptions ---------------------------------------------

    def test_event_actor_mismatch_fails_closed(self):
        def corrupt(con):
            con.execute("UPDATE repair_events SET actor='intruder' WHERE task_id=1 AND sequence=2")
        self._assert_fails_closed(self._current_v6_with_chain("rows_event_actor.sqlite", corrupt=corrupt))

    def test_event_timestamp_mismatch_fails_closed(self):
        # The evidence event's timestamp no longer equals the task's refreshed step time.
        def corrupt(con):
            con.execute("UPDATE repair_events SET created_at='2026-06-05T23:00:00Z' WHERE task_id=1 AND sequence=2")
        self._assert_fails_closed(self._current_v6_with_chain("rows_event_ts.sqlite", corrupt=corrupt))

    def test_incomplete_event_prefix_fails_closed(self):
        # A resolved task missing its final revalidated event: the trail is no
        # longer the legal prefix for the task's state.
        def corrupt(con):
            con.execute("DELETE FROM repair_events WHERE task_id=1 AND sequence=3")
        self._assert_fails_closed(self._current_v6_with_chain("rows_incomplete_prefix.sqlite", corrupt=corrupt))

    # -- deterministic decision-snapshot corruptions (SEC-P30 blocker 5) ----------

    def test_arbitrary_verdict_fails_closed(self):
        # A stored verdict the evaluator never produces for the decision's inputs.
        def corrupt(con):
            con.execute("UPDATE profile_decisions SET verdict='TOTALLY_FABRICATED_VERDICT' WHERE id=1")
        self._assert_fails_closed(self._current_v6_with_chain("rows_bad_verdict.sqlite", corrupt=corrupt))

    def test_arbitrary_decision_text_fails_closed(self):
        # Tampered decision text (a class/text field) inconsistent with the evaluator.
        def corrupt(con):
            con.execute("UPDATE profile_decisions SET repair_text='tampered repair text' WHERE id=2")
        self._assert_fails_closed(self._current_v6_with_chain("rows_bad_text.sqlite", corrupt=corrupt))

    def test_arbitrary_class_fails_closed(self):
        def corrupt(con):
            con.execute("UPDATE profile_decisions SET token_class='selected' WHERE id=1")
        self._assert_fails_closed(self._current_v6_with_chain("rows_bad_class.sqlite", corrupt=corrupt))

    def test_baseline_evidence_mismatch_fails_closed(self):
        # A baseline whose evidence no longer equals the profile's intrinsic
        # constituent fields (intrinsic allowlist is 'stale').
        def corrupt(con):
            con.execute("UPDATE profile_decisions SET evidence_allowlist_status='current' WHERE id=1")
        self._assert_fails_closed(self._current_v6_with_chain("rows_baseline_evidence.sqlite", corrupt=corrupt))

    def test_revalidation_evidence_mismatch_with_task_fails_closed(self):
        # The resolving decision's evidence snapshot no longer equals its task's
        # refreshed evidence (a supported enum, so only the correspondence catches it).
        def corrupt(con):
            con.execute("UPDATE repair_tasks SET refreshed_allowlist_status='stale' WHERE id=1")
        self._assert_fails_closed(self._current_v6_with_chain("rows_reval_task_evidence.sqlite", corrupt=corrupt))

    def test_revalidation_timestamp_mismatch_with_resolved_at_fails_closed(self):
        # The resolving decision's created_at no longer equals the task's resolved_at.
        def corrupt(con):
            con.execute("UPDATE profile_decisions SET created_at=? WHERE id=2", (_T2,))
        self._assert_fails_closed(self._current_v6_with_chain("rows_reval_ts.sqlite", corrupt=corrupt))

    def test_decision_output_inconsistent_with_evaluator_fails_closed(self):
        # Evidence that is a supported enum but whose stored decision does not match
        # the evaluator output over it (payload flipped without updating the verdict).
        def corrupt(con):
            con.execute("UPDATE profile_decisions SET evidence_payload_status='incomplete' WHERE id=2")
            con.execute("UPDATE repair_tasks SET refreshed_payload_status='incomplete' WHERE id=1")
        self._assert_fails_closed(self._current_v6_with_chain("rows_eval_inconsistent.sqlite", corrupt=corrupt))

    # -- live repair must sit on an active profile (SEC-P30 blocker 2) ------------

    def test_live_task_on_non_active_profile_fails_closed(self):
        # A superseded profile that still carries a live (open) repair task: it must
        # be impossible to both supersede a profile and leave a live repair on it.
        def corrupt(con):
            # profile 2 (backfilled active over endpoint 2) becomes superseded by 1.
            con.execute("UPDATE endpoint_profiles SET lifecycle_state='superseded', superseded_by=1 WHERE id=2")
            baseline = _eval_decision(authority="current", allowlist="stale", payload="complete")
            self._insert_decision(con, did=3, profile_id=2, version=1, previous=None, origin="baseline",
                                  authority="current", allowlist="stale", payload="complete",
                                  created_at=_T1, decision=baseline)
            con.execute(
                "INSERT INTO repair_tasks(id, tenant_id, profile_id, actor, state, opened_decision_id, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (2, migrate.SYNTHETIC_TENANT_ID, 2, "ops_analyst", "open", 3, _T1),
            )
            con.execute(
                "INSERT INTO repair_events(id, tenant_id, profile_id, task_id, sequence, event_type, actor, decision_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (4, migrate.SYNTHETIC_TENANT_ID, 2, 2, 1, "repair_opened", "ops_analyst", 3, _T1),
            )
        self._assert_fails_closed(self._current_v6_with_chain("rows_live_on_superseded.sqlite", corrupt=corrupt))

    def test_resolved_task_on_draft_profile_fails_closed(self):
        # A draft cannot have repair history: open_repair_task only opens on an
        # active profile. Even a fully coherent resolved task/decision/event chain
        # is therefore impossible when attached to a draft and must fail closed.
        def corrupt(con):
            con.execute(
                "UPDATE endpoint_profiles SET lifecycle_state='draft', superseded_by=NULL WHERE id=1"
            )
        self._assert_fails_closed(
            self._current_v6_with_chain("rows_resolved_on_draft.sqlite", corrupt=corrupt)
        )

    def test_resolved_task_on_superseded_profile_stays_queryable(self):
        # Historical RESOLVED tasks/decisions on a superseded profile remain valid
        # and queryable. Supersession is the sole legal non-active state for prior
        # repair history; draft profiles can never have opened a repair.
        def corrupt(con):
            con.execute("UPDATE endpoint_profiles SET lifecycle_state='superseded', superseded_by=2 WHERE id=1")
            con.execute("UPDATE endpoint_profiles SET superseded_by=NULL WHERE id=2")
        # profile 1 keeps its resolved task + full chain; superseding it must not
        # invalidate that history, so the DB still reports already_current.
        path = self._current_v6_with_chain("rows_resolved_on_superseded.sqlite", corrupt=corrupt)
        self.assertEqual(migrate.migrate(path)["status"], "already_current")


if __name__ == "__main__":
    unittest.main(verbosity=2)
