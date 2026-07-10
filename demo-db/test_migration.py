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
GOOD_LEDGER = [(migrate.CURRENT_SCHEMA_VERSION, migrate.FOUNDATION_MIGRATION_NAME, SYN_TIME)]

# Every business table a v3->v4 foundation upgrade must leave byte-for-byte intact.
BUSINESS_TABLES = [
    "institutions", "legal_entities", "settlement_endpoints", "route_policies",
    "demo_scenarios", "policy_checks", "route_decisions", "audit_events",
    "operator_actions", "source_manifest",
]

# Minimal standalone shapes mirroring seed.py's columns for the two tables whose
# preservation the invariants name. No FK REFERENCES: the fixture is a disposable
# stand-in for persistent local state, not a full seeded graph.
_FIXTURE_DDL = """
CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
        self.assertEqual(result["applied"], [migrate.FOUNDATION_MIGRATION_NAME])

        # Version markers advanced and now consistent.
        uv, sm = read_markers(path)
        self.assertEqual(uv, migrate.CURRENT_SCHEMA_VERSION)
        self.assertEqual(sm, str(migrate.CURRENT_SCHEMA_VERSION))

        # Ledger metadata recorded.
        ledger = read_rows(path, "schema_migrations", order_by="version")
        self.assertEqual([(v, n) for (v, n, _at) in ledger], migrate.current_ledger())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
