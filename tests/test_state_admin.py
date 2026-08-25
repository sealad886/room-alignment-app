from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from room_alignment.store import Store
from scripts.state_admin import backup, dry_run_migration, restore


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


if __name__ == "__main__":
    unittest.main()
