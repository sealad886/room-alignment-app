import tempfile
import unittest
from pathlib import Path

from room_alignment.models import MediaRecord, ScanSummary
from room_alignment.render import PreflightError, RenderManager, build_manifest, preflight
from room_alignment.store import Store


class StoreAndPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()
        (self.root / "one.mp4").write_bytes(b"test")
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        record = MediaRecord("media-1", "lib", "one.mp4", 4, 1, duration=10, camera="Door")
        self.store.save_scan(ScanSummary("lib", str(self.root), 1, 1, 0, ["Door"], {}), [record])

    def tearDown(self):
        self.temp.cleanup()

    def project(self):
        return {"id":"p1","name":"Test","libraryId":"lib","videoSegments":[{"id":"V-1","mediaId":"media-1","start":0,"end":5,"sourceIn":0}],"audioSegments":[{"id":"A-1","mediaId":None,"start":0,"end":5,"sourceIn":0,"linked":False}]}

    def test_valid_project_and_manifest_preserve_silence_provenance(self):
        project = self.project()
        self.assertTrue(preflight(self.store, project)["valid"])
        manifest = build_manifest(self.store, project)
        self.assertEqual(manifest["audioSegments"][0]["provenance"]["source"], "silence")
        self.assertTrue(manifest["fidelity"]["sourceMediaUnchanged"])

    def test_gap_blocks_render(self):
        project = self.project()
        project["videoSegments"][0]["start"] = 1
        result = preflight(self.store, project)
        self.assertFalse(result["valid"])
        self.assertIn("gap", {item["kind"] for item in result["issues"]})

    def test_source_overrun_blocks_render(self):
        project = self.project()
        project["videoSegments"][0]["end"] = 11
        result = preflight(self.store, project)
        self.assertIn("missing-coverage", {item["kind"] for item in result["issues"]})

    def test_existing_output_is_not_overwritten(self):
        output = Path(self.temp.name) / "existing.mp4"
        output.write_bytes(b"keep")
        with self.assertRaisesRegex(PreflightError, "already exists"):
            RenderManager(self.store).start(self.project(), output)
        self.assertEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
