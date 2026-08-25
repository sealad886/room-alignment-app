from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from room_alignment.scanner import iter_scan_records, quick_fingerprint


class ScannerSafetyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
