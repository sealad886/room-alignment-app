from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from room_alignment.models import MediaRecord
from room_alignment.scanner import iter_scan_records, quick_fingerprint


class ScannerSafetyTests(unittest.TestCase):
    def test_unknown_extension_with_media_signature_is_admitted_for_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            opaque = root / "opaque-payload.data"
            opaque.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 64)
            with patch("room_alignment.scanner.probe", return_value=({}, [], "unsupported test fixture")):
                records = list(iter_scan_records(root, "library", probe_workers=1))
            self.assertEqual([item.relative_path for item in records], [opaque.name])
            self.assertEqual(records[0].warning, "unsupported test fixture")

    def test_symlink_escape_is_warning_bearing_and_never_probed(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            external = Path(outside) / "external.mp4"
            external.write_bytes(b"not authorized")
            (root / "linked.mp4").symlink_to(external)
            with patch("room_alignment.scanner.probe") as probe:
                records = list(iter_scan_records(root, "library", probe_workers=2))
            self.assertEqual(len(records), 1)
            self.assertIn("outside", records[0].warning)
            probe.assert_not_called()

    def test_changed_sidecar_invalidates_incremental_probe_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            sidecar = media.with_suffix(".json")
            sidecar.write_text('{"camera":"one"}', encoding="utf-8")
            existing = {
                "id": "asset",
                "library_id": "library",
                "relative_path": "clip.mp4",
                "size": media.stat().st_size,
                "modified_ns": media.stat().st_mtime_ns,
                "fingerprint": quick_fingerprint(media),
                "evidence": [],
            }
            sidecar.write_text('{"camera":"different and larger"}', encoding="utf-8")
            with patch("room_alignment.scanner.probe", return_value=({}, [], None)) as probe:
                records = list(
                    iter_scan_records(
                        root,
                        "library",
                        mode="INCREMENTAL",
                        existing_lookup=lambda _path: existing,
                        probe_workers=1,
                    )
                )
            self.assertEqual(len(records), 1)
            probe.assert_called_once()
            self.assertEqual(records[0].camera, "different and larger")

    def test_unchanged_incremental_asset_reuses_cached_probe_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "clip.mp4"
            media.write_bytes(b"media")
            existing = {
                "id": "asset",
                "library_id": "library",
                "relative_path": "clip.mp4",
                "size": media.stat().st_size,
                "modified_ns": media.stat().st_mtime_ns,
                "fingerprint": quick_fingerprint(media),
                "evidence": [],
            }
            with patch("room_alignment.scanner.probe") as probe:
                records = list(
                    iter_scan_records(
                        root,
                        "library",
                        mode="INCREMENTAL",
                        existing_lookup=lambda _path: existing,
                        probe_workers=1,
                    )
                )
            probe.assert_not_called()
            self.assertEqual([record.id for record in records], ["asset"])

    def test_closing_scan_generator_stops_owned_probe_work_promptly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4):
                (root / f"{index}.mp4").write_bytes(b"media")
            second_started = threading.Event()
            stopped = threading.Event()

            def controlled_scan(_root, path, library_id, canceled):
                if path.name == "0.mp4":
                    second_started.wait(1)
                else:
                    second_started.set()
                    while not canceled():
                        time.sleep(0.01)
                    stopped.set()
                return MediaRecord(path.name, library_id, path.name, 5, 1)

            with patch("room_alignment.scanner._scan_path", side_effect=controlled_scan):
                records = iter_scan_records(root, "library", probe_workers=2)
                next(records)
                records.close()
                self.assertTrue(stopped.wait(1))


if __name__ == "__main__":
    unittest.main()
