from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from room_alignment.alignment import (
    AudioSignature,
    analyze_project_alignment,
    candidate_pairs,
    correlate_audio,
    estimate_drift_ppm,
)
from room_alignment.domain import (
    apply_command,
    alignment_summary,
    compile_program,
    generate_program_draft,
    new_project,
    project_preparation,
    timeline_section_proposal,
    timeline_window,
)
from room_alignment.models import MediaRecord
from room_alignment.store import Store


def media(asset_id: str, captured_at: str, duration_us: int = 30_000_000) -> dict:
    return {
        "id": asset_id,
        "library_id": "library",
        "relative_path": f"{asset_id}.mp4",
        "captured_at": captured_at,
        "durationUs": duration_us,
        "audio_codec": "aac",
        "streams": [{"id": f"{asset_id}-audio", "codecType": "audio", "sampleRate": 48_000}],
        "fingerprint": {"sampleSha256": asset_id * 8, "size": 100},
    }


def pulse_signal(length: int = 12_000) -> list[int]:
    randomizer = random.Random(7)
    values = [0] * length
    for position in range(100, length - 100, 157):
        amplitude = randomizer.randint(8_000, 25_000)
        for offset, value in enumerate(
            [0, amplitude // 4, amplitude, amplitude // 2, -amplitude // 3, 0]
        ):
            values[position + offset] = value
    return values


class FakeSignatureCache:
    def __init__(self, signatures: dict[str, AudioSignature]):
        self.signatures = signatures

    def signature(self, asset: dict, canceled=None) -> AudioSignature:
        return self.signatures[asset["id"]]


class AudioAlignmentAlgorithmTests(unittest.TestCase):
    def test_envelope_and_gcc_phat_recover_right_clip_correction(self) -> None:
        base = pulse_signal()
        shifted = [0] * 80 + base[:-80]
        evidence = correlate_audio(
            AudioSignature("left", 400, tuple(base), False),
            AudioSignature("right", 400, tuple(shifted), False),
            0,
            0,
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertLessEqual(abs(evidence.correction_us + 200_000), 5_000)
        self.assertGreaterEqual(evidence.confidence, 0.75)
        self.assertLessEqual(abs(evidence.confirmation_delta_us), 5_000)

    def test_correlation_normalizes_overlap_for_unequal_signatures(self) -> None:
        base = pulse_signal(length=2400)
        shifted = [0] * 80 + base[:-80]
        evidence = correlate_audio(
            AudioSignature("left", 400, tuple(base[:1800]), False),
            AudioSignature("right", 400, tuple(shifted), False),
            0,
            0,
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertLessEqual(abs(evidence.correction_us + 200_000), 5_000)
        self.assertGreaterEqual(evidence.confidence, 0.75)
        self.assertGreater(evidence.overlap_us, 0)

    def test_drift_requires_multiple_separated_consistent_anchors(self) -> None:
        self.assertIsNone(estimate_drift_ppm([(0, 0)]))
        self.assertEqual(
            estimate_drift_ppm([(0, 0), (40_000_000, 40_000), (80_000_000, 80_000)]),
            1_000,
        )
        self.assertIsNone(
            estimate_drift_ppm([(0, 0), (40_000_000, 40_000), (80_000_000, 500_000)])
        )

    def test_candidate_generation_is_overlap_bounded_and_cross_source_only(self) -> None:
        assets = [media(f"asset-{index}", "2025-10-15T12:00:00+00:00") for index in range(200)]
        project = new_project(
            "Many clips", "library", assets, initialize_legacy_program=False
        )
        pairs = candidate_pairs(project, {item["id"]: item for item in assets}, max_per_clip=2)
        counts: dict[str, int] = {}
        for left, right in pairs:
            self.assertNotEqual(
                left["clip"]["logicalSourceId"], right["clip"]["logicalSourceId"]
            )
            for item in (left, right):
                clip_id = item["clip"]["id"]
                counts[clip_id] = counts.get(clip_id, 0) + 1
        self.assertTrue(pairs)
        self.assertLessEqual(max(counts.values()), 2)
        self.assertLessEqual(len(pairs), 200)

    def test_proposal_set_aggregates_audio_and_timestamp_review_outcomes(self) -> None:
        assets = [
            media("left", "2025-10-15T12:00:00+00:00"),
            media("right", "2025-10-15T12:00:00+00:00"),
            {**media("silent", "2025-10-15T12:01:00+00:00"), "audio_codec": None, "streams": []},
        ]
        project = new_project(
            "Evidence", "library", assets, initialize_legacy_program=False
        )
        base = pulse_signal()
        shifted = [0] * 80 + base[:-80]
        signatures = FakeSignatureCache(
            {
                "left": AudioSignature("left", 400, tuple(base), False),
                "right": AudioSignature("right", 400, tuple(shifted), False),
            }
        )
        result = analyze_project_alignment(
            project, {item["id"]: item for item in assets}, signatures
        )
        self.assertEqual(result["summary"]["audioConfirmed"], 2)
        self.assertEqual(result["summary"]["timestampOnly"], 1)
        self.assertEqual(len(result["proposals"]), 3)
        self.assertEqual(
            {item["classification"] for item in result["proposals"]},
            {"AUDIO_CONFIRMED", "TIMESTAMP_ONLY"},
        )
        self.assertTrue(
            all(
                item["automaticallyAcceptable"]
                for item in result["proposals"]
                if item["classification"] == "AUDIO_CONFIRMED"
            )
        )


class EvidenceTimelineTests(unittest.TestCase):
    def test_evidence_extent_does_not_collapse_to_empty_program_duration(self) -> None:
        assets = [
            media("first", "2025-10-15T12:00:00+00:00", 60_000_000),
            media("last", "2025-10-15T13:15:00+00:00", 60_000_000),
        ]
        project = new_project(
            "Long evidence", "library", assets, initialize_legacy_program=False
        )
        by_id = {item["id"]: item for item in assets}
        summary = alignment_summary(project, by_id)
        self.assertEqual(summary["evidenceSpan"]["durationUs"], 4_560_000_000)
        self.assertEqual(summary["proposedOutputDurationUs"], 4_560_000_000)
        self.assertFalse(summary["readyForProgramDraft"])
        window = timeline_window(project, by_id, 0, 4_560_000_000, 1_000_000)
        self.assertEqual(window["mode"], "EXACT")
        self.assertEqual(len(window["items"]), 2)
        self.assertTrue(all(item["alignmentState"] == "PROVISIONAL" for item in window["items"]))

    def test_large_timeline_aggregates_with_hard_item_ceiling(self) -> None:
        assets = [
            media(
                f"asset-{index}",
                f"2025-10-15T12:{index // 60:02d}:{index % 60:02d}+00:00",
                2_000_000,
            )
            for index in range(2_100)
        ]
        project = new_project(
            "Large", "library", assets, initialize_legacy_program=False
        )
        window = timeline_window(
            project,
            {item["id"]: item for item in assets},
            0,
            3_600_000_000,
            1_000,
        )
        self.assertEqual(window["mode"], "AGGREGATED")
        self.assertLessEqual(len(window["items"]), 2_000)
        self.assertEqual(window["totalInWindow"], 2_100)

    def test_unresolved_clips_remain_visible_outside_the_aligned_clock(self) -> None:
        unresolved = {**media("unknown", ""), "captured_at": None}
        aligned = media("known", "2025-10-15T12:00:00+00:00")
        project = new_project(
            "Review queue",
            "library",
            [unresolved, aligned],
            initialize_legacy_program=False,
        )
        window = timeline_window(
            project,
            {"unknown": unresolved, "known": aligned},
            0,
            30_000_000,
            100_000,
        )
        self.assertEqual(window["unplacedCount"], 1)
        self.assertEqual(window["unplacedItems"][0]["assetId"], "unknown")
        self.assertEqual(
            len(window["items"]) + len(window["unplacedItems"]),
            2,
        )


class ProgramCompositionTests(unittest.TestCase):
    def project_with_recorded_gap(self) -> tuple[dict, dict[str, dict]]:
        assets = [
            {**media("a-early", "2025-10-15T12:00:00+00:00", 10_000_000), "sourceCandidateId": "a"},
            {**media("b-early", "2025-10-15T12:00:00+00:00", 10_000_000), "sourceCandidateId": "b"},
            {**media("a-late", "2025-10-15T12:00:20+00:00", 10_000_000), "sourceCandidateId": "a"},
            {**media("b-late", "2025-10-15T12:00:20+00:00", 10_000_000), "sourceCandidateId": "b"},
        ]
        project = new_project(
            "Two events",
            "library",
            assets,
            initialize_legacy_program=False,
            source_groups=[
                {"label": "A", "assetIds": ["a-early", "a-late"]},
                {"label": "B", "assetIds": ["b-early", "b-late"]},
            ],
        )
        for clip in project["clips"]:
            clip["alignmentState"] = "ACCEPTED"
            clip["alignmentConfidence"] = 0.95 if clip["assetId"].startswith("a-") else 0.8
            clip["alignmentEvidence"] = ["audio-correlation"]
        return project, {item["id"]: item for item in assets}

    def test_excluded_gap_composes_later_evidence_onto_contiguous_program_clock(self) -> None:
        project, assets = self.project_with_recorded_gap()
        proposal = timeline_section_proposal(project, assets, "EXCLUDE")
        self.assertEqual(proposal["keepDurationUs"], 20_000_000)
        self.assertEqual(proposal["excludedDurationUs"], 10_000_000)
        draft = generate_program_draft(
            project,
            assets,
            {
                "alignmentDigest": proposal["alignmentDigest"],
                "selectionDigest": project["selectionSnapshot"]["digest"],
                "gapMode": "EXCLUDE",
                "sectionProposalDigest": proposal["digest"],
                "replaceExisting": False,
            },
        )
        self.assertEqual(project["timelineSections"], [])
        self.assertEqual(draft["timelineSections"], proposal["sections"])
        compiled = compile_program(draft, assets)
        self.assertTrue(compiled["valid"], compiled["issues"])
        self.assertEqual(compiled["durationUs"], 20_000_000)
        late = next(item for item in compiled["videoSlices"] if item["startUs"] == 10_000_000)
        self.assertEqual(late["startAlignedUs"], 20_000_000)
        self.assertEqual(late["sourceStartUs"], 0)

    def test_slate_gap_generates_provenance_video_and_deliberate_silence(self) -> None:
        project, assets = self.project_with_recorded_gap()
        proposal = timeline_section_proposal(project, assets, "SLATE")
        draft = generate_program_draft(
            project,
            assets,
            {
                "alignmentDigest": proposal["alignmentDigest"],
                "selectionDigest": project["selectionSnapshot"]["digest"],
                "gapMode": "SLATE",
                "sectionProposalDigest": proposal["digest"],
                "replaceExisting": False,
            },
        )
        compiled = compile_program(draft, assets)
        self.assertTrue(compiled["valid"], compiled["issues"])
        self.assertEqual(compiled["durationUs"], 30_000_000)
        slate = next(item for item in compiled["videoSlices"] if item.get("synthetic"))
        self.assertEqual((slate["startUs"], slate["endUs"]), (10_000_000, 20_000_000))
        silence = next(item for item in compiled["audioSlices"] if item.get("synthetic"))
        self.assertEqual((silence["startUs"], silence["endUs"]), (10_000_000, 20_000_000))

    def test_truncated_legacy_program_cannot_enter_cut_or_review(self) -> None:
        project, assets = self.project_with_recorded_gap()
        source_id = project["logicalSources"][0]["id"]
        project["videoBlocks"] = [
            {
                "id": "legacy-video",
                "startUs": 0,
                "endUs": 10_000_000,
                "logicalSourceId": source_id,
                "pinnedClipId": project["clips"][0]["id"],
            }
        ]
        project["audioBlocks"] = [
            {
                "id": "legacy-audio",
                "startUs": 0,
                "endUs": 10_000_000,
                "mode": "FOLLOW_VIDEO",
                "logicalSourceId": None,
                "clipId": None,
                "offsetUs": 0,
                "ratePpm": 0,
            }
        ]
        preparation = project_preparation(project, assets)
        self.assertTrue(preparation["legacyProgramTruncation"])
        self.assertFalse(preparation["canEnterCut"])
        self.assertFalse(preparation["canEnterReview"])
        self.assertIn(
            "PROGRAM_TRUNCATES_ALIGNED_MEDIA",
            {item["code"] for item in preparation["blockers"]},
        )

    def test_explicit_excluded_sections_are_not_legacy_truncation(self) -> None:
        project, assets = self.project_with_recorded_gap()
        for clip in project["clips"]:
            clip["alignmentState"] = "ACCEPTED"
        proposal = timeline_section_proposal(project, assets, "EXCLUDE")
        draft = generate_program_draft(
            project,
            assets,
            {
                "alignmentDigest": proposal["alignmentDigest"],
                "selectionDigest": project["selectionSnapshot"]["digest"],
                "gapMode": "EXCLUDE",
                "sectionProposalDigest": proposal["digest"],
                "replaceExisting": False,
            },
        )
        preparation = project_preparation(draft, assets)
        self.assertLess(preparation["programDurationUs"], preparation["alignment"]["evidenceSpan"]["durationUs"])
        self.assertFalse(preparation["legacyProgramTruncation"])
        self.assertTrue(preparation["canEnterCut"])


class ProposalSetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "source"
        self.source.mkdir()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        grant = self.store.create_grant(self.source, "READ_ONLY_SOURCE")
        self.library = self.store.create_library(grant["id"])

    def test_server_owned_high_confidence_acceptance_is_one_project_revision(self) -> None:
        (self.source / "clip.mp4").write_bytes(b"media")
        record = MediaRecord(
            "asset",
            self.library["id"],
            "clip.mp4",
            5,
            1,
            duration=5,
            duration_us=5_000_000,
            captured_at="2025-10-15T12:00:00+00:00",
            audio_codec="aac",
            fingerprint={"size": 5, "modifiedNs": 1},
        )
        scan = self.store.begin_scan(self.library["id"], "FULL")
        self.store.save_media_batch(scan["id"], [record])
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 1})
        project = self.store.create_project("Evidence", self.library["id"], ["asset"])
        clip = project["clips"][0]
        proposal_set = {
            "id": "set-one",
            "projectId": project["id"],
            "projectRevision": project["revision"],
            "selectionDigest": project["selectionSnapshot"]["digest"],
            "inputDigest": "a" * 64,
            "digest": "b" * 64,
            "algorithm": "bounded-audio-evidence-graph",
            "algorithmVersion": "1",
            "config": {},
            "configDigest": "c" * 64,
            "status": "PENDING",
            "summary": {},
            "proposals": [
                {
                    "id": "proposal-one",
                    "clipId": clip["id"],
                    "assetId": "asset",
                    "classification": "AUDIO_CONFIRMED",
                    "proposedAlignment": {
                        "anchorSourceUs": 0,
                        "anchorAlignedUs": 125_000,
                        "ratePpm": 0,
                    },
                    "confidence": 0.9,
                    "automaticallyAcceptable": True,
                    "requiresDriftConfirmation": False,
                    "evidence": [],
                    "limitations": [],
                    "inputFingerprintDigest": "d" * 64,
                },
                {
                    "id": "proposal-review",
                    "clipId": clip["id"],
                    "assetId": "asset",
                    "classification": "TIMESTAMP_ONLY",
                    "proposedAlignment": {
                        "anchorSourceUs": 0,
                        "anchorAlignedUs": 0,
                        "ratePpm": 0,
                    },
                    "confidence": 0.55,
                    "automaticallyAcceptable": False,
                    "requiresDriftConfirmation": False,
                    "evidence": [],
                    "limitations": ["timestamp-only placement requires review"],
                    "inputFingerprintDigest": "d" * 64,
                },
            ],
            "limitations": [],
            "createdAt": "2025-10-15T12:00:00+00:00",
            "updatedAt": "2025-10-15T12:00:00+00:00",
        }
        self.store.save_alignment_proposal_set(proposal_set)
        result = self.store.apply_project_command(
            project["id"],
            {
                "commandId": "accept-set",
                "expectedRevision": project["revision"],
                "commandType": "AcceptAlignmentProposalSet",
                "payload": {
                    "proposalSetId": "set-one",
                    "digest": "b" * 64,
                    "mode": "HIGH_CONFIDENCE",
                },
            },
        )
        self.assertEqual(result["appliedRevision"], project["revision"] + 1)
        accepted = result["project"]["clips"][0]
        self.assertEqual(accepted["alignment"]["anchorAlignedUs"], 125_000)
        self.assertEqual(accepted["alignmentState"], "ACCEPTED")
        self.assertIn("audio-correlation", accepted["alignmentEvidence"])
        partially_resolved = self.store.alignment_proposal_set("set-one")
        self.assertEqual(partially_resolved["status"], "PARTIALLY_RESOLVED")
        self.assertEqual(partially_resolved["acceptedProposalIds"], ["proposal-one"])
        rejected = self.store.apply_project_command(
            project["id"],
            {
                "commandId": "reject-review-proposal",
                "expectedRevision": result["appliedRevision"],
                "commandType": "RejectAlignmentProposal",
                "payload": {
                    "proposalSetId": "set-one",
                    "proposalId": "proposal-review",
                    "digest": "b" * 64,
                },
            },
        )
        self.assertEqual(rejected["appliedRevision"], project["revision"] + 2)
        resolved = self.store.alignment_proposal_set("set-one")
        self.assertEqual(resolved["status"], "RESOLVED")
        self.assertEqual(resolved["acceptedProposalIds"], ["proposal-one"])
        self.assertEqual(resolved["rejectedProposalIds"], ["proposal-review"])


if __name__ == "__main__":
    unittest.main()
