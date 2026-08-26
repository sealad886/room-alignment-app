from __future__ import annotations

import errno
import json
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from room_alignment.domain import DomainError, digest_json, opaque_id
from room_alignment.models import MediaRecord
from room_alignment.render import (
    CanonicalRenderManager,
    RunningJob,
    _promote_no_replace,
    build_render_plan,
    build_v1_ffmpeg_command,
    build_v1_manifest,
)
from room_alignment.scanner import probe, quick_fingerprint
from room_alignment.store import Store


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CanonicalRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.output = Path(self.temp.name) / "output"
        self.output.mkdir()
        self.media_path = self.root / "source.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x224466:s=320x180:r=24:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(self.media_path),
            ],
            check=True,
        )
        values, evidence, warning = probe(self.media_path)
        self.assertIsNone(warning)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        source_grant = self.store.create_grant(self.root, "READ_ONLY_SOURCE")
        output_grant = self.store.create_grant(self.output, "WRITE_OUTPUT")
        self.output_grant_id = output_grant["id"]
        library = self.store.create_library(source_grant["id"])
        scan = self.store.begin_scan(library["id"], "FULL")
        record = MediaRecord(
            "asset",
            library["id"],
            self.media_path.name,
            self.media_path.stat().st_size,
            self.media_path.stat().st_mtime_ns,
            camera="Door",
            evidence=evidence,
            fingerprint=quick_fingerprint(self.media_path),
            **values,
        )
        self.store.save_media_batch(scan["id"], [record])
        self.store.scan_progress(scan["id"])
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 1})
        self.project = self.store.create_project("Event", library["id"], ["asset"])

    def tearDown(self):
        self.temp.cleanup()

    def test_reviewed_plan_renders_video_and_manifest_pair(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "program.mp4", "profile": "COMPATIBLE"},
        )
        self.assertEqual(plan["status"], "READY", plan["issues"])
        manifest_preview = build_v1_manifest(
            plan,
            {"id": "artifact", "videoDigest": "a" * 64, "manifestDigest": "b" * 64},
        )
        self.assertIsNone(manifest_preview["artifact"]["manifestSha256"])
        self.store.attest_review(plan["id"], plan["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        started = manager.start(plan["id"])
        deadline = time.monotonic() + 20
        job = self.store.job(started["job"]["id"])
        while time.monotonic() < deadline:
            job = self.store.job(started["job"]["id"])
            if job["status"] in {"SUCCEEDED", "FAILED", "CANCELED"}:
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "SUCCEEDED", job)
        artifact = self.store.artifact(started["artifact"]["id"])
        self.assertEqual(artifact["status"], "COMPLETE")
        video = self.output / "program.mp4"
        manifest_path = self.output / "program.mp4.manifest.json"
        self.assertTrue(video.is_file())
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["renderPlan"]["digest"], plan["planDigest"])
        self.assertEqual(manifest["videoSlices"][0]["assetId"], "asset")
        self.assertTrue(manifest["videoSlices"][0]["streamId"])
        self.assertTrue(manifest["audioSlices"][0]["streamId"])
        self.assertIn("ffmpeg", manifest["toolVersions"])
        self.assertEqual(len(manifest["manifestCanonicalContentSha256"]), 64)
        canonical_digest = manifest.pop("manifestCanonicalContentSha256")
        self.assertEqual(manifest["manifestCanonicalization"], "room-alignment-canonical-json/v1")
        self.assertEqual(canonical_digest, digest_json(manifest))
        self.assertTrue(manifest["fidelity"]["sourceFilesModified"] is False)

    def test_project_change_invalidates_reviewed_plan(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "stale.mp4", "profile": "COMPATIBLE"},
        )
        self.store.attest_review(plan["id"], plan["warningCodes"])
        self.store.apply_project_command(
            self.project["id"],
            {
                "commandId": "rename",
                "expectedRevision": self.project["revision"],
                "commandType": "UpdateProjectMetadata",
                "payload": {"name": "Changed"},
            },
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            CanonicalRenderManager(self.store).start(plan["id"])

    def test_existing_destination_blocks_plan(self):
        (self.output / "existing.mp4").write_bytes(b"keep")
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "existing.mp4", "profile": "COMPATIBLE"},
        )
        self.assertEqual(plan["status"], "BLOCKED")
        self.assertIn("DESTINATION_EXISTS", {item["code"] for item in plan["issues"]})

    def test_identical_render_plan_request_is_idempotent(self):
        settings = {
            "outputGrantId": self.output_grant_id,
            "filename": "idempotent.mp4",
            "profile": "COMPATIBLE",
        }
        first = build_render_plan(self.store, self.project["id"], settings)
        second = build_render_plan(self.store, self.project["id"], settings)
        self.assertEqual(first["planDigest"], second["planDigest"])
        self.assertEqual(first["id"], second["id"])

    def test_render_normalization_rejects_invalid_dimensions_and_frame_rates(self):
        invalid_settings = [
            {"width": 15},
            {"height": 15},
            {"frameRate": 0},
            {"frameRate": 0.5},
            {"frameRate": 241},
            {"frameRate": float("nan")},
            {"width": "1920"},
            {"width": True},
            {"width": False},
            {"height": True},
            {"height": False},
            {"frameRate": True},
            {"frameRate": False},
        ]
        for index, invalid in enumerate(invalid_settings):
            with self.subTest(invalid=invalid), self.assertRaises(DomainError) as raised:
                build_render_plan(
                    self.store,
                    self.project["id"],
                    {
                        "outputGrantId": self.output_grant_id,
                        "filename": f"invalid-{index}.mp4",
                        "profile": "COMPATIBLE",
                        **invalid,
                    },
                )
            self.assertEqual(raised.exception.code, "VALIDATION_FAILED")

    def test_reviewed_plan_rejects_a_source_rename_instead_of_following_asset_path(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "renamed.mp4", "profile": "COMPATIBLE"},
        )
        renamed_path = self.media_path.with_name("source-renamed.mp4")
        self.media_path.rename(renamed_path)
        values, evidence, warning = probe(renamed_path)
        self.assertIsNone(warning)
        scan = self.store.begin_scan(self.project["libraryId"], "FULL")
        record = MediaRecord(
            "path-derived-renamed-asset",
            self.project["libraryId"],
            renamed_path.name,
            renamed_path.stat().st_size,
            renamed_path.stat().st_mtime_ns,
            camera="Door",
            evidence=evidence,
            fingerprint=quick_fingerprint(renamed_path),
            **values,
        )
        self.store.save_media_batch(scan["id"], [record])
        self.store.scan_progress(scan["id"])
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 1})
        self.assertEqual(self.store.media_record("asset")["relative_path"], renamed_path.name)
        with self.assertRaises(DomainError) as raised:
            CanonicalRenderManager(self.store)._validate_sources(plan)
        self.assertEqual(raised.exception.code, "SOURCE_CHANGED")

    def test_reviewed_plan_reports_a_deleted_source_as_changed(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "deleted.mp4", "profile": "COMPATIBLE"},
        )
        self.media_path.unlink()
        with self.assertRaises(DomainError) as raised:
            CanonicalRenderManager(self.store)._validate_sources(plan)
        self.assertEqual(raised.exception.code, "SOURCE_CHANGED")

    def test_archival_profile_declares_and_builds_lossless_output(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "archive.mkv", "profile": "ARCHIVAL_LOSSLESS"},
        )
        self.assertEqual(plan["status"], "READY", plan["issues"])
        command = build_v1_ffmpeg_command(self.store, plan, self.output / ".archive.partial.mkv")
        self.assertIn("ffv1", command)
        self.assertIn("pcm_s24le", command)
        with self.assertRaisesRegex(ValueError, "must end with .mkv"):
            build_render_plan(
                self.store,
                self.project["id"],
                {"outputGrantId": self.output_grant_id, "filename": "archive.mp4", "profile": "ARCHIVAL_LOSSLESS"},
            )

    def test_provenance_correction_invalidates_plan_and_review(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "provenance.mp4", "profile": "COMPATIBLE"},
        )
        self.store.resolve_provenance("asset", "captured_at", {"utc": "2026-08-25T12:00:00Z"}, "Corrected", "test")
        changed = self.store.project(self.project["id"])
        self.assertEqual(changed["provenanceRevision"], 1)
        with self.assertRaisesRegex(ValueError, "Provenance"):
            self.store.attest_review(plan["id"], plan["warningCodes"])

    def test_review_attestation_serializes_project_validation_and_update(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "atomic-review.mp4", "profile": "COMPATIBLE"},
        )
        attestation_entered = threading.Event()
        release_attestation = threading.Event()
        command_done = threading.Event()
        failures: list[BaseException] = []

        def blocking_id(prefix):
            if prefix == "review":
                attestation_entered.set()
                if not release_attestation.wait(2):
                    raise RuntimeError("test did not release attestation")
            return opaque_id(prefix)

        def attest():
            try:
                self.store.attest_review(plan["id"], plan["warningCodes"])
            except BaseException as error:  # pragma: no cover - reported by the parent assertion
                failures.append(error)

        def edit_project():
            try:
                self.store.apply_project_command(
                    self.project["id"],
                    {
                        "commandId": "edit-during-attestation",
                        "expectedRevision": self.project["revision"],
                        "commandType": "UpdateProjectMetadata",
                        "payload": {"name": "Concurrent edit retained"},
                    },
                )
            except BaseException as error:  # pragma: no cover - reported by the parent assertion
                failures.append(error)
            finally:
                command_done.set()

        with patch("room_alignment.store.opaque_id", side_effect=blocking_id):
            attest_thread = threading.Thread(target=attest)
            attest_thread.start()
            self.assertTrue(attestation_entered.wait(1))
            command_thread = threading.Thread(target=edit_project)
            command_thread.start()
            self.assertFalse(command_done.wait(0.2), "project edit bypassed attestation transaction")
            release_attestation.set()
            attest_thread.join(2)
            command_thread.join(2)

        self.assertFalse(attest_thread.is_alive())
        self.assertFalse(command_thread.is_alive())
        self.assertEqual(failures, [])
        current = self.store.project(self.project["id"])
        self.assertEqual(current["revision"], self.project["revision"] + 1)
        self.assertEqual(current["name"], "Concurrent edit retained")
        self.assertIsNone(current["review"])
        self.assertEqual(
            self.store.project_revision(self.project["id"], current["revision"])["name"],
            "Concurrent edit retained",
        )

    def test_cancel_before_process_launch_is_terminal_and_creates_no_output(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "cancel.mp4", "profile": "COMPATIBLE"},
        )
        self.store.attest_review(plan["id"], plan["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.threading.Thread.start"):
            started = manager.start(plan["id"])
        manager.cancel(started["job"]["id"])
        manager.cancel(started["job"]["id"])
        manager._run(started["job"]["id"], started["artifact"]["id"], plan)
        self.assertEqual(self.store.job(started["job"]["id"])["status"], "CANCELED")
        self.assertFalse((self.output / "cancel.mp4").exists())

    def test_cancel_during_promotion_preserves_recoverable_output(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "late-cancel.mp4", "profile": "COMPATIBLE"},
        )
        self.store.attest_review(plan["id"], plan["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.threading.Thread.start"):
            started = manager.start(plan["id"])
        real_promote = _promote_no_replace
        canceled = False

        def cancel_then_promote(source, destination):
            nonlocal canceled
            if not canceled:
                canceled = True
                manager.cancel(started["job"]["id"])
            return real_promote(source, destination)

        with patch("room_alignment.render._promote_no_replace", side_effect=cancel_then_promote):
            manager._run(started["job"]["id"], started["artifact"]["id"], plan)
        self.assertEqual(self.store.job(started["job"]["id"])["status"], "CANCELED")
        self.assertEqual(self.store.artifact(started["artifact"]["id"])["status"], "FAILED_RECOVERABLE")
        self.assertTrue((self.output / "late-cancel.mp4").exists())
        self.assertFalse((self.output / "late-cancel.mp4.manifest.json").exists())

    def test_output_grant_revocation_during_promotion_preserves_recoverable_output(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "late-revoke.mp4", "profile": "COMPATIBLE"},
        )
        self.store.attest_review(plan["id"], plan["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.threading.Thread.start"):
            started = manager.start(plan["id"])
        real_promote = _promote_no_replace
        revoked = False

        def revoke_then_promote(source, destination):
            nonlocal revoked
            if not revoked:
                revoked = True
                self.store.revoke_grant(self.output_grant_id)
            return real_promote(source, destination)

        with patch("room_alignment.render._promote_no_replace", side_effect=revoke_then_promote):
            manager._run(started["job"]["id"], started["artifact"]["id"], plan)
        job = self.store.job(started["job"]["id"])
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["errorCode"], "GRANT_REQUIRED")
        self.assertEqual(self.store.artifact(started["artifact"]["id"])["status"], "FAILED_RECOVERABLE")
        self.assertTrue((self.output / "late-revoke.mp4").exists())
        self.assertFalse((self.output / "late-revoke.mp4.manifest.json").exists())

    def test_late_video_collision_is_never_overwritten(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "video-collision.mp4", "profile": "COMPATIBLE"},
        )
        self.store.attest_review(plan["id"], plan["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.threading.Thread.start"):
            started = manager.start(plan["id"])
        final = self.output / "video-collision.mp4"
        sentinel = b"external video"
        real_promote = _promote_no_replace

        def collide_then_promote(source, destination):
            final.write_bytes(sentinel)
            return real_promote(source, destination)

        with patch("room_alignment.render._promote_no_replace", side_effect=collide_then_promote):
            manager._run(started["job"]["id"], started["artifact"]["id"], plan)
        self.assertEqual(final.read_bytes(), sentinel)
        self.assertEqual(self.store.job(started["job"]["id"])["errorCode"], "DESTINATION_EXISTS")
        self.assertEqual(self.store.artifact(started["artifact"]["id"])["status"], "FAILED")
        self.assertFalse((self.output / "video-collision.mp4.manifest.json").exists())

    def test_late_manifest_collision_preserves_both_visible_files_as_recoverable(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "manifest-collision.mp4", "profile": "COMPATIBLE"},
        )
        self.store.attest_review(plan["id"], plan["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.threading.Thread.start"):
            started = manager.start(plan["id"])
        manifest = self.output / "manifest-collision.mp4.manifest.json"
        sentinel = b"external manifest"
        real_promote = _promote_no_replace
        calls = 0

        def collide_on_manifest(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                manifest.write_bytes(sentinel)
            return real_promote(source, destination)

        with patch("room_alignment.render._promote_no_replace", side_effect=collide_on_manifest):
            manager._run(started["job"]["id"], started["artifact"]["id"], plan)
        self.assertTrue((self.output / "manifest-collision.mp4").exists())
        self.assertEqual(manifest.read_bytes(), sentinel)
        self.assertEqual(self.store.job(started["job"]["id"])["errorCode"], "DESTINATION_EXISTS")
        self.assertEqual(
            self.store.artifact(started["artifact"]["id"])["status"], "FAILED_RECOVERABLE"
        )

    def test_published_output_mutation_before_completion_is_detected(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {
                "outputGrantId": self.output_grant_id,
                "filename": "mutated-after-publish.mp4",
                "profile": "COMPATIBLE",
            },
        )
        self.store.attest_review(plan["id"], plan["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.threading.Thread.start"):
            started = manager.start(plan["id"])
        final = self.output / "mutated-after-publish.mp4"
        real_stopped = manager._finalization_stopped
        checks = 0

        def mutate_after_manifest(*args, **kwargs):
            nonlocal checks
            checks += 1
            stopped = real_stopped(*args, **kwargs)
            if checks == 3:
                final.write_bytes(b"external replacement")
            return stopped

        with patch.object(manager, "_finalization_stopped", side_effect=mutate_after_manifest):
            manager._run(started["job"]["id"], started["artifact"]["id"], plan)

        self.assertEqual(self.store.job(started["job"]["id"])["errorCode"], "DESTINATION_EXISTS")
        self.assertEqual(
            self.store.artifact(started["artifact"]["id"])["status"], "FAILED_RECOVERABLE"
        )
        self.assertEqual(final.read_bytes(), b"external replacement")

    def test_output_grant_revocation_stops_queued_render_as_failed(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "revoked.mp4", "profile": "COMPATIBLE"},
        )
        self.store.attest_review(plan["id"], plan["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.threading.Thread.start"):
            started = manager.start(plan["id"])
        self.store.revoke_grant(self.output_grant_id)
        manager._run(started["job"]["id"], started["artifact"]["id"], plan)
        job = self.store.job(started["job"]["id"])
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["errorCode"], "GRANT_REQUIRED")
        self.assertEqual(self.store.artifact(started["artifact"]["id"])["status"], "FAILED")
        self.assertFalse((self.output / "revoked.mp4").exists())

    def test_render_manager_backpressures_a_second_render(self):
        first = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "first.mp4", "profile": "COMPATIBLE"},
        )
        second = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "second.mp4", "profile": "COMPATIBLE"},
        )
        self.store.attest_review(first["id"], first["warningCodes"])
        self.store.attest_review(second["id"], second["warningCodes"])
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.threading.Thread.start"):
            manager.start(first["id"])
            with self.assertRaisesRegex(ValueError, "one render"):
                manager.start(second["id"])

    def test_startup_quarantines_exact_owned_partial(self):
        plan = build_render_plan(
            self.store,
            self.project["id"],
            {"outputGrantId": self.output_grant_id, "filename": "recover.mp4", "profile": "COMPATIBLE"},
        )
        artifact = self.store.create_artifact(plan["id"], self.output_grant_id, "recover.mp4")
        token = artifact["id"].rsplit("_", 1)[-1]
        partial = self.output / f".recover.partial.{token}.mp4"
        partial.write_bytes(b"partial")
        CanonicalRenderManager(self.store)
        recovered = self.store.artifact(artifact["id"])
        self.assertEqual(recovered["status"], "FAILED_RECOVERABLE")
        self.assertFalse(partial.exists())
        quarantine = Path(self.store.path).parent / "recovery"
        self.assertEqual(len(list(quarantine.iterdir())), 1)

    def test_render_process_stop_sends_term_once_then_kill_once(self):
        process = Mock(pid=1234)
        running = RunningJob(process)
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.time.monotonic", side_effect=[10, 11, 16]), patch(
            "room_alignment.render.os.killpg"
        ) as killpg:
            manager._request_process_stop(running)
            manager._request_process_stop(running)
            manager._request_process_stop(running)
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(killpg.call_args_list[0].args[1], signal.SIGTERM)
        self.assertEqual(killpg.call_args_list[1].args[1], signal.SIGKILL)

    def test_concurrent_process_stop_sends_each_signal_once(self):
        process = Mock(pid=1234)
        running = RunningJob(process)
        manager = CanonicalRenderManager(self.store)
        with patch("room_alignment.render.os.killpg") as killpg:
            threads = [
                threading.Thread(target=manager._request_process_stop, args=(running, 0))
                for _index in range(20)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual({call.args[1] for call in killpg.call_args_list}, {signal.SIGTERM, signal.SIGKILL})

    def test_cross_filesystem_quarantine_falls_back_to_move(self):
        source = self.output / "partial"
        destination = Path(self.temp.name) / "recovery" / "partial"
        source.write_bytes(b"partial")
        destination.parent.mkdir()
        cross_device = OSError(errno.EXDEV, "cross-device")
        with patch("room_alignment.render.os.replace", side_effect=cross_device), patch(
            "room_alignment.render.shutil.move"
        ) as move:
            CanonicalRenderManager._quarantine_partial(source, destination)
        move.assert_called_once_with(str(source), str(destination))


if __name__ == "__main__":
    unittest.main()
