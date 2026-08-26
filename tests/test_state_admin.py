from __future__ import annotations

import os
import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from room_alignment.state_admin import backup, dry_run_migration, restore
from room_alignment.store import Store


class StateAdministrationTests(unittest.TestCase):
    def test_backup_dry_run_and_restore_are_integrity_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state" / "room-alignment.sqlite3"
            store = Store(state)
            first = root / "first-source"
            first.mkdir()
            store.create_grant(first, "READ_ONLY_SOURCE")
            backup_file = root / "backup.sqlite3"
            self.assertEqual(backup(state, backup_file)["integrity"], "ok")
            self.assertEqual(dry_run_migration(backup_file)["integrity"], "ok")

            second = root / "second-source"
            second.mkdir()
            store.create_grant(second, "READ_ONLY_SOURCE")
            self.assertEqual(len(store.grants()), 2)
            restored = restore(state, backup_file, True)
            self.assertEqual(restored["integrity"], "ok")
            self.assertEqual(len(Store(state).grants()), 1)
            self.assertTrue(any(state.parent.glob("*.pre-restore-*")))

    def test_restore_requires_explicit_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state.sqlite3"
            Store(state)
            backup_file = root / "backup.sqlite3"
            backup(state, backup_file)
            with self.assertRaisesRegex(ValueError, "--replace"):
                restore(state, backup_file, False)

    def test_restore_removes_sidecars_from_replaced_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state.sqlite3"
            Store(state)
            backup_file = root / "backup.sqlite3"
            backup(state, backup_file)
            real_replace = os.replace

            def replace_then_create_stale_sidecars(source: Path, destination: Path) -> None:
                real_replace(source, destination)
                Path(f"{destination}-wal").write_bytes(b"stale wal")
                Path(f"{destination}-shm").write_bytes(b"stale shm")

            with patch("room_alignment.state_admin.os.replace", side_effect=replace_then_create_stale_sidecars):
                restore(state, backup_file, replace=True)
            for suffix, stale in (("-wal", b"stale wal"), ("-shm", b"stale shm")):
                sidecar = Path(f"{state}{suffix}")
                if sidecar.exists():
                    self.assertNotEqual(sidecar.read_bytes(), stale)

    def test_read_only_sqlite_uri_handles_reserved_path_characters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state ? # directory"
            root.mkdir()
            state = root / "room #1?.sqlite3"
            Store(state)
            backup_file = root / "backup ? #.sqlite3"
            self.assertEqual(backup(state, backup_file)["integrity"], "ok")
            self.assertEqual(dry_run_migration(backup_file)["integrity"], "ok")

    def test_failed_staged_migration_keeps_original_and_removes_staging_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.sqlite3"
            connection = sqlite3.connect(state)
            connection.execute("CREATE TABLE legacy(value TEXT)")
            connection.execute("INSERT INTO legacy(value) VALUES('preserve')")
            connection.commit()
            connection.close()
            with patch.object(Store, "_ensure_legacy_columns", side_effect=RuntimeError("injected failure")):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    Store(state)
            preserved = sqlite3.connect(state)
            try:
                self.assertEqual(preserved.execute("SELECT value FROM legacy").fetchone()[0], "preserve")
            finally:
                preserved.close()
            self.assertFalse(list(state.parent.glob(".state.sqlite3.migration-*")))
            self.assertTrue(list(state.parent.glob("state.sqlite3.backup-v0-*")))

    def test_dry_run_adds_legacy_columns_before_creating_new_indexes(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.sqlite3"
            connection = sqlite3.connect(state)
            connection.executescript(
                """
                CREATE TABLE media (
                  id TEXT PRIMARY KEY,
                  library_id TEXT NOT NULL,
                  relative_path TEXT NOT NULL,
                  captured_at TEXT,
                  camera TEXT,
                  duration REAL,
                  first_generation INTEGER NOT NULL DEFAULT 0,
                  last_generation INTEGER NOT NULL DEFAULT 0,
                  missing INTEGER NOT NULL DEFAULT 0,
                  fingerprint_json TEXT NOT NULL DEFAULT '{}',
                  record_json TEXT NOT NULL
                );
                PRAGMA user_version=3;
                """
            )
            connection.close()

            result = dry_run_migration(state)

            self.assertEqual(result["integrity"], "ok")
            self.assertEqual(result["schemaVersion"], 8)


if __name__ == "__main__":
    unittest.main()
