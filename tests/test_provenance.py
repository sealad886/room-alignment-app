import json
import tempfile
import unittest
from pathlib import Path

from room_alignment.provenance import infer_from_path, merge_evidence, read_sidecar


class ProvenanceTests(unittest.TestCase):
    def test_infers_common_but_not_vendor_specific_filename(self):
        root = Path("archive")
        path = root / "yard" / "2026-08-25_19-42-08_North_Gate_0042.mp4"
        values, evidence = infer_from_path(path, path.relative_to(root))
        self.assertEqual(values["captured_at"], "2026-08-25T19:42:08")
        self.assertEqual(values["camera"], "North Gate")
        self.assertEqual(values["sequence"], "0042")
        self.assertTrue(any(item.kind == "filename" for item in evidence))

    def test_unknown_filename_is_accepted_with_folder_context(self):
        root = Path("archive")
        path = root / "Custom Input" / "opaque-file.mp4"
        values, _ = infer_from_path(path, path.relative_to(root))
        self.assertEqual(values["camera"], "Opaque File")
        self.assertNotIn("captured_at", values)

    def test_composes_parent_date_and_filename_time_without_vendor_contract(self):
        root = Path("archive")
        path = root / "24-10" / "24-10-29" / "11-55-35_FrontdoorG8T1K00132550188_001.mp4"
        values, evidence = infer_from_path(path, path.relative_to(root))
        self.assertEqual(values["captured_at"], "2024-10-29T11:55:35")
        self.assertEqual(values["camera"], "Frontdoor")
        self.assertEqual(values["sequence"], "001")
        self.assertEqual({item.kind for item in evidence if item.field.startswith("captured_at")}, {"filesystem", "filename", "importer"})

    def test_sidecar_preserves_unknown_fields_and_wins_by_confidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"")
            path.with_suffix(".json").write_text(json.dumps({"deviceName": "Porch", "vendor_blob": {"x": 1}}))
            sidecar = read_sidecar(path, Path("clip.mp4"))
            inferred = ({"camera": "Clip"}, [])
            values, evidence = merge_evidence(inferred, sidecar)
            self.assertEqual(values["camera"], "Porch")
            self.assertEqual(values["custom"]["vendor_blob"], {"x": 1})
            self.assertEqual(evidence[0].kind, "sidecar")


if __name__ == "__main__":
    unittest.main()
