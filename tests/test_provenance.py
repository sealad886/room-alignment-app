import json
import tempfile
import unittest
from pathlib import Path

from room_alignment import __version__
from room_alignment.models import ProvenanceEvidence
from room_alignment.provenance import infer_from_path, merge_evidence, normalize_timestamp, read_sidecar
from room_alignment.scanner import media_record_from_dict


class ProvenanceTests(unittest.TestCase):
    def test_default_extractor_version_matches_the_package(self):
        evidence = ProvenanceEvidence("user", "camera", "Door", 1, "test")
        self.assertEqual(evidence.extractor_version, __version__)

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

    def test_invalid_zone_key_is_disclosed_without_raising(self):
        outcome = normalize_timestamp("2026-01-01T00:00:00", "../UTC")
        self.assertEqual(outcome["ambiguity"], "INVALID_TIME_ZONE")
        self.assertIsNone(outcome["resolvedUtc"])

    def test_timestamp_policy_covers_explicit_unambiguous_and_unparseable_values(self):
        explicit = normalize_timestamp("2026-01-02T03:04:05+02:00", "Europe/Dublin")
        self.assertEqual(explicit["ambiguity"], "EXPLICIT_OFFSET")
        self.assertTrue(explicit["timezoneExplicit"])
        self.assertEqual(explicit["resolvedUtc"], "2026-01-02T01:04:05Z")

        unambiguous = normalize_timestamp("2026-06-01T12:00:00", "Europe/Dublin")
        self.assertEqual(unambiguous["ambiguity"], "UNAMBIGUOUS_LOCAL_TIME")
        self.assertIsNotNone(unambiguous["resolvedUtc"])

        unparseable = normalize_timestamp("not a time", "UTC")
        self.assertEqual(unparseable["ambiguity"], "UNPARSEABLE")
        self.assertIsNone(unparseable["resolvedUtc"])

    def test_future_evidence_fields_survive_under_custom_metadata(self):
        record = media_record_from_dict(
            {
                "id": "asset",
                "library_id": "library",
                "relative_path": "clip.mp4",
                "size": 1,
                "modified_ns": 1,
                "evidence": [
                    {
                        "kind": "importer",
                        "field": "captured_at",
                        "value": "raw",
                        "confidence": 0.5,
                        "origin": "future",
                        "futureField": {"preserve": True},
                    }
                ],
            }
        )
        self.assertEqual(record.evidence[0].custom["futureField"], {"preserve": True})
        self.assertEqual(record.to_dict()["evidence"][0]["custom"]["futureField"], {"preserve": True})


if __name__ == "__main__":
    unittest.main()
