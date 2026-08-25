from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from room_alignment.models import MediaRecord
from room_alignment.render import CanonicalRenderManager, build_render_plan
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


if __name__ == "__main__":
    unittest.main()
