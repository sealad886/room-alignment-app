from __future__ import annotations

import errno
import json
import signal
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from room_alignment.domain import digest_json
from room_alignment.models import MediaRecord
from room_alignment.render import CanonicalRenderManager, RunningJob, build_render_plan, build_v1_ffmpeg_command
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
        manager._run(started["job"]["id"], started["artifact"]["id"], plan)
        self.assertEqual(self.store.job(started["job"]["id"])["status"], "CANCELED")
        self.assertFalse((self.output / "cancel.mp4").exists())

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
        with patch("room_alignment.render.time.monotonic", side_effect=[10, 11, 16]), patch(
            "room_alignment.render.os.killpg"
        ) as killpg:
            CanonicalRenderManager._request_process_stop(running)
            CanonicalRenderManager._request_process_stop(running)
            CanonicalRenderManager._request_process_stop(running)
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(killpg.call_args_list[0].args[1], signal.SIGTERM)
        self.assertEqual(killpg.call_args_list[1].args[1], signal.SIGKILL)

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
