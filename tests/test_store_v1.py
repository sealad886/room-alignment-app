from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from room_alignment.domain import DomainError
from room_alignment.models import MediaRecord
from room_alignment.scanner import quick_fingerprint
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
        self.assertIn("affectedIntervals", first)
        self.assertEqual(self.store.project_revision(project["id"], 1)["name"], "Event")
        self.assertEqual(self.store.project_revision(project["id"], 2)["name"], "Renamed")
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
        self.assertEqual(self.store.events(job_id=scan["id"])[-1]["details"]["reason"], "GRANT_REQUIRED")
        with self.assertRaisesRegex(DomainError, "revoked"):
            self.store.library_root(self.library["id"])

    def test_unambiguous_rename_preserves_asset_identity(self):
        original_path = self.root / "old-name.mp4"
        original_path.write_bytes(b"stable source bytes")
        first = MediaRecord(
            "asset-original",
            self.library["id"],
            original_path.name,
            original_path.stat().st_size,
            original_path.stat().st_mtime_ns,
            duration=1,
            duration_us=1_000_000,
            fingerprint=quick_fingerprint(original_path),
        )
        self.scan("FULL", [first])
        renamed_path = original_path.with_name("new-name.mp4")
        original_path.rename(renamed_path)
        renamed = MediaRecord(
            "path-derived-new-id",
            self.library["id"],
            renamed_path.name,
            renamed_path.stat().st_size,
            renamed_path.stat().st_mtime_ns,
            duration=1,
            duration_us=1_000_000,
            fingerprint=quick_fingerprint(renamed_path),
        )
        self.scan("FULL", [renamed])
        self.assertEqual(self.store.media_record("asset-original")["relative_path"], "new-name.mp4")
        with self.assertRaises(DomainError):
            self.store.media_record("path-derived-new-id")

    def test_library_time_policy_renormalizes_without_losing_raw_evidence(self):
        media = self.record("clock", "clock.mp4")
        media.captured_at = "2026-10-25T01:30:00"
        self.scan("FULL", [media])
        first = self.store.media_record("clock")
        self.assertEqual(first["custom"]["timestampPolicy"]["ambiguity"], "AMBIGUOUS_FOLD")
        self.assertEqual(first["custom"]["timestampPolicy"]["rawValue"], "2026-10-25T01:30:00")
        self.store.update_library_time_policy(self.library["id"], "Europe/Dublin", 1, "REJECT")
        second = self.store.media_record("clock")
        self.assertNotEqual(first["captured_at"], second["captured_at"])
        self.assertEqual(second["custom"]["timestampPolicy"]["rawValue"], "2026-10-25T01:30:00")

    def test_legacy_project_migration_preserves_unknowns_and_requires_explicit_silence(self):
        first = self.record("first", "first.mp4")
        second = self.record("second", "second.mp4")
        first.source_candidate_id = second.source_candidate_id = "candidate-same"
        self.scan("FULL", [first, second])
        legacy = {
            "id": "legacy-project",
            "name": "Legacy",
            "libraryId": self.library["id"],
            "revision": 7,
            "alignment": {"first": {"offsetMs": 25}, "second": {"offsetMs": 50}},
            "videoSegments": [
                {"id": "v1", "mediaId": "first", "start": "0.0000005", "end": "1.2345675"}
            ],
            "audioSegments": [
                {"id": "implicit-null", "mediaId": None, "start": 0, "end": 1, "linked": False},
                {"id": "explicit-silence", "mediaId": None, "start": 0, "end": 1, "silence": True},
            ],
            "unknownRecoveryField": {"retain": True},
            "review": {"ready": True},
        }
        self.store.save_project(legacy)
        migrated = self.store.project("legacy-project")
        self.assertEqual(len(migrated["logicalSources"]), 2)
        self.assertEqual(migrated["videoBlocks"][0]["startUs"], 0)
        self.assertEqual(migrated["videoBlocks"][0]["endUs"], 1_234_568)
        self.assertEqual([item["id"] for item in migrated["audioBlocks"]], ["explicit-silence"])
        self.assertTrue(migrated["legacy"]["unknownRecoveryField"]["retain"])
        self.assertEqual(migrated["migration"]["rounding"], "half-even")
        self.assertIsNone(migrated["review"])


if __name__ == "__main__":
    unittest.main()
