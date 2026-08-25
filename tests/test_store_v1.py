from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from room_alignment.domain import DomainError
from room_alignment.models import MediaRecord
from room_alignment.store import Store


class StoreV1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.output = Path(self.temp.name) / "output"
        self.output.mkdir()
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        grant = self.store.create_grant(self.root, "READ_ONLY_SOURCE")
        self.library = self.store.create_library(grant["id"], "Europe/Dublin")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, media_id: str, path: str) -> MediaRecord:
        (self.root / path).write_bytes(media_id.encode())
        return MediaRecord(
            media_id,
            self.library["id"],
            path,
            len(media_id),
            1,
            duration=5,
            duration_us=5_000_000,
            camera="Door",
            audio_codec="aac",
            fingerprint={"size": len(media_id), "modifiedNs": 1},
        )

    def scan(self, mode: str, records: list[MediaRecord], limit: int | None = None):
        scan = self.store.begin_scan(self.library["id"], mode, limit)
        self.store.save_media_batch(scan["id"], records)
        for record in records:
            self.store.scan_progress(scan["id"], warning=bool(record.warning))
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": len(records)})
        return self.store.scan(scan["id"])

    def test_source_and_output_grants_cannot_overlap(self):
        nested = self.root / "renders"
        nested.mkdir()
        with self.assertRaisesRegex(DomainError, "overlap"):
            self.store.create_grant(nested, "WRITE_OUTPUT")
        output = self.store.create_grant(self.output, "WRITE_OUTPUT")
        self.assertEqual(output["role"], "WRITE_OUTPUT")

    def test_only_successful_full_scan_marks_unseen_assets_missing(self):
        one = self.record("one", "one.mp4")
        two = self.record("two", "two.mp4")
        self.scan("FULL", [one, two])
        self.scan("BOUNDED", [one], 1)
        self.assertFalse(self.store.media_record("two")["missing"])
        self.scan("FULL", [one])
        self.assertTrue(self.store.media_record("two")["missing"])

    def test_media_cursor_stays_on_snapshot_and_excludes_new_records(self):
        self.scan("FULL", [self.record("a", "a.mp4"), self.record("b", "b.mp4")])
        first = self.store.media_page(self.library["id"], limit=1)
        self.scan("INCREMENTAL", [self.record("c", "c.mp4")])
        second = self.store.media_page(
            self.library["id"],
            limit=10,
            cursor=first["nextCursor"],
            generation=first["snapshotGeneration"],
        )
        self.assertEqual([item["id"] for item in second["items"]], ["b"])

    def test_commands_are_atomic_idempotent_and_revision_checked(self):
        media = self.record("one", "one.mp4")
        self.scan("FULL", [media])
        project = self.store.create_project("Event", self.library["id"], ["one"])
        envelope = {
            "commandId": "command-1",
            "expectedRevision": project["revision"],
            "commandType": "UpdateProjectMetadata",
            "payload": {"name": "Renamed"},
        }
        first = self.store.apply_project_command(project["id"], envelope)
        repeated = self.store.apply_project_command(project["id"], envelope)
        self.assertEqual(first, repeated)
        self.assertEqual(first["appliedRevision"], 2)
        with self.assertRaisesRegex(DomainError, "already used"):
            self.store.apply_project_command(
                project["id"],
                {**envelope, "payload": {"name": "Different"}},
            )
        with self.assertRaisesRegex(DomainError, "changed"):
            self.store.apply_project_command(
                project["id"],
                {
                    "commandId": "command-2",
                    "expectedRevision": 1,
                    "commandType": "UpdateProjectMetadata",
                    "payload": {"name": "Stale"},
                },
            )

    def test_restart_marks_running_render_recoverable(self):
        job = self.store.create_job("RENDER")
        self.store.transition_job(job["id"], "RUNNING", 0.2, "Rendering")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.job(job["id"])["status"], "FAILED_RECOVERABLE")
        events = reopened.events(job_id=job["id"])
        self.assertEqual(events[-1]["eventType"], "RECOVERY")

    def test_grant_revocation_interrupts_dependent_scan(self):
        scan = self.store.begin_scan(self.library["id"], "FULL")
        self.store.revoke_grant(self.library["sourceGrantId"])
        self.assertEqual(self.store.job(scan["id"])["status"], "INTERRUPTED")
        with self.assertRaisesRegex(DomainError, "revoked"):
            self.store.library_root(self.library["id"])


if __name__ == "__main__":
    unittest.main()
