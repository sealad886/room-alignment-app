from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_canceled_full_scan_cannot_commit_success_or_reuse_generation(self):
        one = self.record("one", "one.mp4")
        two = self.record("two", "two.mp4")
        first = self.scan("FULL", [one, two])
        interrupted = self.store.begin_scan(self.library["id"], "FULL")
        self.store.save_media_batch(interrupted["id"], [one])
        self.store.scan_progress(interrupted["id"])
        self.store.cancel_scan(interrupted["id"])
        self.store.finish_scan(interrupted["id"], "SUCCEEDED", {"videos": 1})
        self.assertEqual(self.store.scan(interrupted["id"])["status"], "CANCELED")
        self.assertEqual(self.store.job(interrupted["id"])["status"], "CANCELED")
        self.assertFalse(self.store.media_record("two")["missing"])
        self.assertEqual(self.store.library(self.library["id"])["currentGeneration"], first["generation"])
        next_scan = self.store.begin_scan(self.library["id"], "INCREMENTAL")
        self.assertEqual(next_scan["generation"], interrupted["generation"] + 1)

    def test_scan_generation_advances_past_migrated_library_counter(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM scan_generations WHERE library_id=?", (self.library["id"],))
            db.execute("UPDATE libraries SET current_generation=7 WHERE id=?", (self.library["id"],))
        self.assertEqual(self.store.begin_scan(self.library["id"], "FULL")["generation"], 8)

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
        other_project = self.store.create_project("Other event", self.library["id"], ["one"])
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
            self.store.apply_project_command(other_project["id"], envelope)
        self.assertEqual(self.store.project(other_project["id"])["name"], "Other event")
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

    def test_restart_reconciles_jobs_orphaned_while_queued(self):
        self.scan("FULL", [self.record("queued-media", "queued.mp4")])
        project = self.store.create_project("Queued", self.library["id"], ["queued-media"])
        output_grant = self.store.create_grant(self.output, "WRITE_OUTPUT")
        plan = self.store.save_render_plan(
            {
                "id": "queued-plan",
                "projectId": project["id"],
                "projectRevision": project["revision"],
                "planDigest": "queued-plan-digest",
                "sourceSetDigest": "queued-sources",
                "provenanceRevision": 0,
                "status": "READY",
            }
        )
        analysis = self.store.create_job("ALIGNMENT_ANALYSIS")
        render = self.store.create_job("RENDER")
        artifact = self.store.create_artifact(plan["id"], output_grant["id"], "queued.mp4")
        self.store.update_artifact(artifact["id"], job_id=render["id"], status="QUEUED")
        scan = self.store.begin_scan(self.library["id"], "INCREMENTAL")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.job(analysis["id"])["status"], "INTERRUPTED")
        self.assertEqual(reopened.job(render["id"])["status"], "FAILED_RECOVERABLE")
        self.assertEqual(reopened.scan(scan["id"])["status"], "INTERRUPTED")
        self.assertEqual(reopened.artifact(artifact["id"])["status"], "FAILED_RECOVERABLE")

    def test_grant_revocation_stops_dependent_scan_with_diagnostic(self):
        scan = self.store.begin_scan(self.library["id"], "FULL")
        self.store.revoke_grant(self.library["sourceGrantId"])
        self.assertEqual(self.store.job(scan["id"])["status"], "CANCEL_REQUESTED")
        self.assertEqual(self.store.job(scan["id"])["errorCode"], "GRANT_REQUIRED")
        self.assertTrue(self.store.scan(scan["id"])["cancelRequested"])
        self.assertEqual(self.store.events(job_id=scan["id"])[-1]["details"]["reason"], "GRANT_REQUIRED")
        with self.assertRaisesRegex(DomainError, "revoked"):
            self.store.library_root(self.library["id"])

    def test_legacy_library_without_grant_is_fail_closed(self):
        with self.store.connect() as db:
            db.execute("UPDATE libraries SET grant_id=NULL WHERE id=?", (self.library["id"],))
        with self.assertRaises(DomainError) as raised:
            self.store.library_root(self.library["id"])
        self.assertEqual(raised.exception.code, "GRANT_REQUIRED")

    def test_project_rejects_media_from_a_different_library(self):
        self.scan("FULL", [self.record("one", "one.mp4")])
        other_root = Path(self.temp.name) / "other-source"
        other_root.mkdir()
        other_grant = self.store.create_grant(other_root, "READ_ONLY_SOURCE")
        other_library = self.store.create_library(other_grant["id"])
        with self.assertRaisesRegex(DomainError, "project library"):
            self.store.create_project("Mixed", other_library["id"], ["one"])

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

    def test_path_swap_preserves_both_asset_identities(self):
        first_path = self.root / "first.mp4"
        second_path = self.root / "second.mp4"
        first_path.write_bytes(b"first stable bytes")
        second_path.write_bytes(b"second stable bytes")

        def media(media_id: str, path: Path) -> MediaRecord:
            return MediaRecord(
                media_id,
                self.library["id"],
                path.name,
                path.stat().st_size,
                path.stat().st_mtime_ns,
                duration=1,
                duration_us=1_000_000,
                fingerprint=quick_fingerprint(path),
            )

        self.scan("FULL", [media("asset-first", first_path), media("asset-second", second_path)])
        temporary = self.root / "swap.tmp"
        first_path.rename(temporary)
        second_path.rename(first_path)
        temporary.rename(second_path)
        scan = self.store.begin_scan(self.library["id"], "FULL")
        self.store.save_media_batch(scan["id"], [media("new-at-first", first_path)])
        self.store.save_media_batch(scan["id"], [media("new-at-second", second_path)])
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 2})
        self.assertEqual(self.store.media_record("asset-second")["relative_path"], "first.mp4")
        self.assertEqual(self.store.media_record("asset-first")["relative_path"], "second.mp4")

    def test_path_collision_without_identity_key_uses_library_root_safely(self):
        first_path = self.root / "first.mp4"
        second_path = self.root / "second.mp4"
        first_path.write_bytes(b"first")
        second_path.write_bytes(b"second")

        def record(media_id: str, path: Path) -> MediaRecord:
            return MediaRecord(
                media_id,
                self.library["id"],
                path.name,
                path.stat().st_size,
                path.stat().st_mtime_ns,
                duration=1,
                duration_us=1_000_000,
                fingerprint={"size": path.stat().st_size, "modifiedNs": path.stat().st_mtime_ns},
            )

        self.scan("FULL", [record("asset-first", first_path), record("asset-second", second_path)])
        incoming = record("asset-first", second_path)
        scan = self.store.begin_scan(self.library["id"], "INCREMENTAL")
        self.store.save_media_batch(scan["id"], [incoming])
        self.store.scan_progress(scan["id"])
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 1})
        self.assertEqual(self.store.media_record("asset-first")["relative_path"], "second.mp4")

    def test_scan_progress_accepts_batched_counts(self):
        scan = self.store.begin_scan(self.library["id"], "FULL")
        self.store.scan_progress(scan["id"], processed=50, warning_count=3, message="Indexing media")
        current = self.store.scan(scan["id"])
        self.assertEqual((current["scanned"], current["videos"], current["warnings"]), (50, 50, 3))

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

    def test_successful_migration_removes_replaced_database_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "legacy.sqlite3"
            connection = sqlite3.connect(state)
            connection.execute("CREATE TABLE legacy(value TEXT)")
            connection.commit()
            connection.close()
            real_replace = os.replace

            def replace_then_create_stale_sidecars(source: Path, destination: Path) -> None:
                real_replace(source, destination)
                Path(f"{destination}-wal").write_bytes(b"stale wal")
                Path(f"{destination}-shm").write_bytes(b"stale shm")

            with patch("room_alignment.store.os.replace", side_effect=replace_then_create_stale_sidecars):
                Store(state)
            for suffix, stale in (("-wal", b"stale wal"), ("-shm", b"stale shm")):
                sidecar = Path(f"{state}{suffix}")
                if sidecar.exists():
                    self.assertNotEqual(sidecar.read_bytes(), stale)

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
        repeated = self.store.project("legacy-project")
        self.assertEqual(
            [item["id"] for item in migrated["logicalSources"]],
            [item["id"] for item in repeated["logicalSources"]],
        )
        self.assertEqual(
            [item["id"] for item in migrated["clips"]],
            [item["id"] for item in repeated["clips"]],
        )

    def test_legacy_job_is_exposed_with_canonical_shape(self):
        self.scan("FULL", [self.record("one", "one.mp4")])
        project = self.store.create_project("Event", self.library["id"], ["one"])
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO render_jobs(id,project_id,status,output_path,progress,message) VALUES(?,?,?,?,?,?)",
                ("legacy-job", project["id"], "complete", "/redacted/output.mp4", 1, "Done"),
            )
        job = self.store.job("legacy-job")
        self.assertEqual(job["kind"], "RENDER")
        self.assertEqual(job["status"], "SUCCEEDED")
        self.assertEqual(job["projectId"], project["id"])
        self.assertEqual(job["checkpoint"], {})

    def test_duplicate_render_plan_digest_returns_original_plan(self):
        self.scan("FULL", [self.record("plan-media", "plan.mp4")])
        project = self.store.create_project("Plan", self.library["id"], ["plan-media"])
        base = {
            "projectId": project["id"],
            "projectRevision": 1,
            "planDigest": "same-digest",
            "sourceSetDigest": "sources",
            "provenanceRevision": 0,
            "status": "READY",
        }
        first = self.store.save_render_plan({"id": "plan-one", **base})
        second = self.store.save_render_plan({"id": "plan-two", **base})
        self.assertEqual(first["id"], "plan-one")
        self.assertEqual(second["id"], "plan-one")

    def test_suggestion_saved_after_project_edit_is_immediately_stale(self):
        self.scan("FULL", [self.record("suggestion-media", "suggestion.mp4")])
        project = self.store.create_project("Suggestion", self.library["id"], ["suggestion-media"])
        self.store.apply_project_command(
            project["id"],
            {
                "commandId": "advance-before-suggestion",
                "expectedRevision": project["revision"],
                "commandType": "UpdateProjectMetadata",
                "payload": {"name": "Advanced"},
            },
        )
        suggestion = self.store.save_suggestion(
            {
                "projectId": project["id"],
                "libraryId": self.library["id"],
                "kind": "ALIGNMENT",
                "inputDigest": "old-project-input",
                "algorithm": "test-alignment",
                "projectRevision": project["revision"],
                "confidence": 0.5,
                "evidence": [],
                "limitations": [],
            }
        )
        self.assertEqual(suggestion["status"], "STALE")
        self.assertIn("revision changed", suggestion["invalidationReason"])
        self.assertEqual(self.store.suggestions(project["id"])[0]["status"], "STALE")

    def test_alignment_acceptance_rejects_client_tampering(self):
        self.scan("FULL", [self.record("suggestion-media", "suggestion.mp4")])
        project = self.store.create_project("Suggestion", self.library["id"], ["suggestion-media"])
        clip = project["clips"][0]
        suggestion = self.store.save_suggestion(
            {
                "projectId": project["id"],
                "libraryId": self.library["id"],
                "kind": "ALIGNMENT",
                "inputDigest": "current-project-input",
                "algorithm": "test-alignment",
                "projectRevision": project["revision"],
                "confidence": 0.5,
                "clipId": clip["id"],
                "sync": {"anchorSourceUs": 0, "anchorOutputUs": 100_000, "ratePpm": 0},
                "evidence": [],
                "limitations": [],
            }
        )
        with self.assertRaisesRegex(DomainError, "canonical evidence"):
            self.store.apply_project_command(
                project["id"],
                {
                    "commandId": "tampered-suggestion",
                    "expectedRevision": project["revision"],
                    "commandType": "AcceptAlignmentSuggestion",
                    "payload": {
                        "suggestionId": suggestion["id"],
                        "clipId": clip["id"],
                        "sync": {"anchorSourceUs": 0, "anchorOutputUs": 999_000, "ratePpm": 0},
                        "confirmDrift": False,
                    },
                },
            )


if __name__ == "__main__":
    unittest.main()
