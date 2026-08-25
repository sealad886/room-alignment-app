import json
import tempfile
import unittest
from pathlib import Path

from room_alignment.provenance import infer_from_path, merge_evidence, normalize_timestamp, read_sidecar


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

    def test_timestamp_policy_discloses_dst_fold_and_nonexistent_time(self):
        first_fold = normalize_timestamp("2026-10-25T01:30:00", "Europe/Dublin", 0, "REJECT")
        second_fold = normalize_timestamp("2026-10-25T01:30:00", "Europe/Dublin", 1, "REJECT")
        self.assertEqual(first_fold["ambiguity"], "AMBIGUOUS_FOLD")
        self.assertNotEqual(first_fold["resolvedUtc"], second_fold["resolvedUtc"])

        rejected = normalize_timestamp("2026-03-29T01:30:00", "Europe/Dublin", 0, "REJECT")
        shifted = normalize_timestamp("2026-03-29T01:30:00", "Europe/Dublin", 0, "SHIFT_FORWARD")
        self.assertEqual(rejected["ambiguity"], "NONEXISTENT_LOCAL_TIME")
        self.assertIsNone(rejected["resolvedUtc"])
        self.assertEqual(shifted["ambiguity"], "NONEXISTENT_SHIFTED_FORWARD")
        self.assertIsNotNone(shifted["resolvedUtc"])


if __name__ == "__main__":
    unittest.main()
