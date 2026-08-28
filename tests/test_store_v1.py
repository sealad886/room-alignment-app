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

    def test_application_settings_persist_with_bounded_values(self):
        updated = self.store.update_application_settings(
            {
                "overlapSearchExtensionUs": 75_000_000,
                "textScalePercent": 115,
                "colorScheme": "SLATE",
                "renderVideoCodec": "HEVC_VIDEOTOOLBOX",
                "renderResolution": "HD_720P",
            }
        )
        self.assertEqual(updated["overlapSearchExtensionUs"], 75_000_000)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.application_settings(), updated)
        with self.assertRaisesRegex(DomainError, "between 0 and 300 seconds"):
            reopened.update_application_settings({"overlapSearchExtensionUs": 300_000_001})
        with self.assertRaisesRegex(DomainError, "Text scale"):
            reopened.update_application_settings({"textScalePercent": 141})
        with self.assertRaisesRegex(DomainError, "color scheme"):
            reopened.update_application_settings({"colorScheme": "UNKNOWN"})
        with self.assertRaisesRegex(DomainError, "hardware video codec"):
            reopened.update_application_settings({"renderVideoCodec": "LIBX264"})
        with self.assertRaisesRegex(DomainError, "render resolution"):
            reopened.update_application_settings({"renderResolution": "8K"})

    def test_overlap_setting_change_stales_pending_alignment_proposals(self):
        record = self.record("settings-clip", "settings-clip.mp4")
        self.scan("FULL", [record])
        project = self.store.create_project("Settings", self.library["id"], [record.id])
        proposal_set = {
            "id": "settings-proposal-set",
            "projectId": project["id"],
            "projectRevision": project["revision"],
            "selectionDigest": project["selectionSnapshot"]["digest"],
            "inputDigest": "a" * 64,
            "digest": "b" * 64,
            "algorithm": "bounded-audio-evidence-graph",
            "algorithmVersion": "2",
            "config": {"overlapSearchExtensionUs": 30_000_000},
            "configDigest": "c" * 64,
            "status": "PENDING",
            "summary": {},
            "proposals": [],
            "limitations": [],
            "createdAt": "2025-10-15T12:00:00+00:00",
            "updatedAt": "2025-10-15T12:00:00+00:00",
        }
        self.store.save_alignment_proposal_set(proposal_set)
        self.store.update_application_settings({"overlapSearchExtensionUs": 60_000_000})
        saved = self.store.alignment_proposal_sets(project["id"])[0]
        self.assertEqual(saved["status"], "STALE")
        self.assertEqual(saved["invalidationReason"], "Overlap search settings changed")

    def test_grant_identity_change_fails_closed(self):
        moved = Path(self.temp.name) / "moved-source"
        self.root.rename(moved)
        self.root.mkdir()
        with self.assertRaisesRegex(DomainError, "identity changed"):
            self.store.library_root(self.library["id"])

    def test_only_successful_full_scan_marks_unseen_assets_missing(self):
        one = self.record("one", "one.mp4")
        two = self.record("two", "two.mp4")
        self.scan("FULL", [one, two])
        self.scan("BOUNDED", [one], 1)
        self.assertFalse(self.store.media_record("two")["missing"])
        self.scan("FULL", [one])
        self.assertTrue(self.store.media_record("two")["missing"])

    def test_multi_root_paths_are_distinct_and_missing_is_root_scoped(self):
        second_root = Path(self.temp.name) / "second-source"
        second_root.mkdir()
        second_grant = self.store.create_grant(second_root, "READ_ONLY_SOURCE")
        second = self.store.add_library_root(self.library["id"], second_grant["id"])
        first = self.store.library(self.library["id"])["roots"][0]
        (self.root / "same.mp4").write_bytes(b"first")
        (second_root / "same.mp4").write_bytes(b"second")
        first_record = MediaRecord(
            "asset-first",
            self.library["id"],
            "same.mp4",
            5,
            1,
            root_id=first["id"],
            fingerprint={"size": 5, "modifiedNs": 1},
        )
        second_record = MediaRecord(
            "asset-second",
            self.library["id"],
            "same.mp4",
            6,
            1,
            root_id=second["id"],
            fingerprint={"size": 6, "modifiedNs": 1},
        )
        scan = self.store.begin_scan(
            self.library["id"], "FULL", root_ids=[first["id"], second["id"]]
        )
        self.store.save_media_batch(scan["id"], [first_record, second_record])
        self.store.scan_progress(scan["id"], processed=1, root_id=first["id"])
        self.store.scan_progress(scan["id"], processed=1, root_id=second["id"])
        self.store.finish_scan_root(
            scan["id"], first["id"], "SUCCEEDED", full_traversal_completed=True
        )
        self.store.finish_scan_root(
            scan["id"], second["id"], "SUCCEEDED", full_traversal_completed=True
        )
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 2})

        page = self.store.media_page(self.library["id"])
        self.assertEqual({item["id"] for item in page["items"]}, {"asset-first", "asset-second"})
        self.assertEqual({item["rootId"] for item in page["items"]}, {first["id"], second["id"]})

        root_only = self.store.begin_scan(self.library["id"], "FULL", root_ids=[first["id"]])
        self.store.finish_scan_root(
            root_only["id"], first["id"], "SUCCEEDED", full_traversal_completed=True
        )
        self.store.finish_scan(root_only["id"], "SUCCEEDED", {"videos": 0})
        self.assertTrue(self.store.media_record("asset-first")["missing"])
        self.assertFalse(self.store.media_record("asset-second")["missing"])

    def test_library_rejects_duplicate_nested_and_overlapping_roots(self):
        nested = self.root / "nested"
        nested.mkdir()
        nested_grant = self.store.create_grant(nested, "READ_ONLY_SOURCE")
        with self.assertRaisesRegex(DomainError, "nested"):
            self.store.add_library_root(self.library["id"], nested_grant["id"])

    def test_revoking_one_library_root_preserves_other_root(self):
        second_root = Path(self.temp.name) / "second-source"
        second_root.mkdir()
        second_grant = self.store.create_grant(second_root, "READ_ONLY_SOURCE")
        second = self.store.add_library_root(self.library["id"], second_grant["id"])
        first = self.store.library(self.library["id"])["roots"][0]
        before_revision = self.store.library(self.library["id"])["catalogRevision"]
        self.store.revoke_library_root(self.library["id"], first["id"])
        active = self.store.active_library_root_paths(self.library["id"])
        self.assertEqual([item[0] for item in active], [second["id"]])
        self.assertFalse(self.store.library_roots(self.library["id"])[0]["active"])
        after_revision = self.store.library(self.library["id"])["catalogRevision"]
        self.assertEqual(after_revision, before_revision + 1)
        self.store.revoke_library_root(self.library["id"], first["id"])
        self.assertEqual(self.store.library(self.library["id"])["catalogRevision"], after_revision)

    def test_disconnected_root_reconnects_with_stable_root_identity(self):
        root = self.store.library(self.library["id"])["roots"][0]
        self.store.revoke_library_root(self.library["id"], root["id"])
        replacement_grant = self.store.create_grant(self.root, "READ_ONLY_SOURCE")
        reconnected = self.store.add_library_root(self.library["id"], replacement_grant["id"])
        self.assertEqual(reconnected["id"], root["id"])
        self.assertTrue(reconnected["active"])
        self.assertEqual(self.store.active_library_root_paths(self.library["id"])[0][0], root["id"])

    def test_root_time_policy_override_is_explicit_and_survives_library_policy_change(self):
        second_root = Path(self.temp.name) / "second-source"
        second_root.mkdir()
        second_grant = self.store.create_grant(second_root, "READ_ONLY_SOURCE")
        second = self.store.add_library_root(
            self.library["id"],
            second_grant["id"],
            time_policy_override={
                "timeZone": "America/New_York",
                "dstFold": 0,
                "nonexistentPolicy": "REJECT",
            },
        )
        (second_root / "local.mp4").write_bytes(b"media")
        record = MediaRecord(
            "local-time",
            self.library["id"],
            "local.mp4",
            5,
            1,
            root_id=second["id"],
            captured_at="2025-10-15T12:00:00",
        )
        scan = self.store.begin_scan(self.library["id"], "FULL", root_ids=[second["id"]])
        self.store.save_media_batch(scan["id"], [record])
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 1})
        self.assertEqual(self.store.media_record("local-time")["captured_at"], "2025-10-15T16:00:00Z")
        self.store.update_library_time_policy(self.library["id"], "Asia/Tokyo")
        resolved = self.store.media_record("local-time")
        self.assertEqual(resolved["captured_at"], "2025-10-15T16:00:00Z")
        self.assertEqual(resolved["custom"]["timestampPolicy"]["timeZone"], "America/New_York")

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
        self.assertEqual(project["videoBlocks"], [])
        self.assertEqual(project["audioBlocks"], [])
        self.assertIsNone(project["programDraft"])
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
        with self.store.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM project_revisions WHERE project_id=?",
                    (project["id"],),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM project_revision_deltas WHERE project_id=?",
                    (project["id"],),
                ).fetchone()[0],
                1,
            )
            self.assertGreater(
                db.execute(
                    "SELECT COUNT(*) FROM project_components WHERE project_id=?",
                    (project["id"],),
                ).fetchone()[0],
                0,
            )
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

    def test_delta_command_returns_only_changed_project_fields(self):
        self.scan("FULL", [self.record("delta", "delta.mp4")])
        project = self.store.create_project("Delta", self.library["id"], ["delta"])
        result = self.store.apply_project_delta_command(
            project["id"],
            {
                "commandId": "delta-command",
                "expectedRevision": project["revision"],
                "commandType": "UpdateProjectMetadata",
                "payload": {"name": "Delta renamed"},
            },
        )
        self.assertNotIn("project", result)
        self.assertEqual(result["changedEntities"]["set"]["name"], "Delta renamed")
        self.assertNotIn("clips", result["changedEntities"]["set"])
        self.assertEqual(result["projectSummary"]["revision"], 2)
        self.assertIn("current", result["issueDelta"])

    def test_restart_marks_running_render_recoverable(self):
        job = self.store.create_job("RENDER")
        self.store.transition_job(job["id"], "RUNNING", 0.2, "Rendering")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.job(job["id"])["status"], "FAILED_RECOVERABLE")
        events = reopened.events(job_id=job["id"])
        self.assertEqual(events[-1]["eventType"], "RECOVERY")

    def test_render_completion_preserves_execution_details(self):
        self.scan("FULL", [self.record("completed-media", "completed.mp4")])
        project = self.store.create_project("Completed", self.library["id"], ["completed-media"])
        output_grant = self.store.create_grant(self.output, "WRITE_OUTPUT")
        plan = self.store.save_render_plan(
            {
                "id": "completed-plan",
                "projectId": project["id"],
                "projectRevision": project["revision"],
                "planDigest": "program-digest",
                "sourceSetDigest": "completed-sources",
                "provenanceRevision": 0,
                "status": "READY",
            }
        )
        job = self.store.create_job("RENDER")
        self.store.transition_job(job["id"], "RUNNING", 0.2, "Rendering")
        artifact = self.store.create_artifact(plan["id"], output_grant["id"], "completed.mp4")
        self.store.update_artifact(
            artifact["id"],
            job_id=job["id"],
            status="RUNNING",
            details_json={
                "programDigest": "program-digest",
                "executionDigest": "execution-digest",
                "renderVideoCodec": "HEVC_VIDEOTOOLBOX",
                "hardwareAccelerated": True,
            },
        )

        completed = self.store.complete_render_artifact(
            job["id"],
            artifact["id"],
            "video-digest",
            "manifest-digest",
            {"videoBytes": 123, "manifestBytes": 456},
        )

        self.assertTrue(completed)
        details = self.store.artifact(artifact["id"])["details"]
        self.assertEqual(details["executionDigest"], "execution-digest")
        self.assertEqual(details["renderVideoCodec"], "HEVC_VIDEOTOOLBOX")
        self.assertTrue(details["hardwareAccelerated"])
        self.assertEqual(details["videoBytes"], 123)

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

    def test_staged_migration_uses_named_rows_for_existing_canonical_state(self):
        state = Path(self.temp.name) / "state.sqlite3"
        connection = sqlite3.connect(state)
        connection.execute("PRAGMA user_version=6")
        connection.commit()
        connection.close()

        reopened = Store(state)

        self.assertEqual(reopened.library(self.library["id"])["id"], self.library["id"])
        self.assertTrue(list(Path(self.temp.name).glob("state.sqlite3.backup-v6-*")))

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
