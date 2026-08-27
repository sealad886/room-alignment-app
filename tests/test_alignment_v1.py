from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from room_alignment.alignment import (
    AudioSignature,
    _huber_graph_adjustments,
    analyze_project_alignment,
    candidate_pairs,
    correlate_audio,
    estimate_drift_ppm,
)
from room_alignment.domain import (
    DomainError,
    apply_command,
    alignment_digest,
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
    def test_outlier_pass_preserves_single_large_supported_correction(self) -> None:
        edge = {
            "leftClipId": "left",
            "rightClipId": "right",
            "correctionUs": 2_000_000,
            "confidence": 0.9,
        }
        adjustments, _support = _huber_graph_adjustments(
            {"left", "right"}, [edge], "left", regularize=False
        )
        self.assertEqual(adjustments["left"], 0)
        self.assertEqual(adjustments["right"], 2_000_000)

    def test_outlier_pass_converges_consistent_long_chain(self) -> None:
        edges = [
            {
                "leftClipId": "a",
                "rightClipId": "b",
                "correctionUs": 2_000_000,
                "confidence": 0.9,
            },
            {
                "leftClipId": "b",
                "rightClipId": "c",
                "correctionUs": 2_000_000,
                "confidence": 0.9,
            },
        ]
        adjustments, _support = _huber_graph_adjustments(
            {"a", "b", "c"}, edges, "a", regularize=False
        )
        self.assertEqual(adjustments, {"a": 0, "b": 2_000_000, "c": 4_000_000})

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

    def test_candidate_generation_extends_search_symmetrically_for_timestamp_skew(self) -> None:
        assets = [
            media("earlier", "2025-10-15T12:00:00+00:00", 5_000_000),
            media("later", "2025-10-15T12:00:40+00:00", 5_000_000),
        ]
        project = new_project("Skewed timestamps", "library", assets, initialize_legacy_program=False)
        by_id = {item["id"]: item for item in assets}
        self.assertEqual(candidate_pairs(project, by_id, uncertainty_us=30_000_000), [])
        pairs = candidate_pairs(project, by_id, uncertainty_us=40_000_000)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(
            {pairs[0][0]["clip"]["assetId"], pairs[0][1]["clip"]["assetId"]},
            {"earlier", "later"},
        )

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
            project,
            {item["id"]: item for item in assets},
            signatures,
            overlap_search_extension_us=45_000_000,
        )
        self.assertEqual(result["config"]["overlapSearchExtensionUs"], 45_000_000)
        self.assertEqual(result["algorithmVersion"], "3")
        self.assertGreaterEqual(result["summary"]["componentCount"], 1)
        audio_proposal = next(
            item for item in result["proposals"] if item["classification"] == "AUDIO_CONFIRMED"
        )
        self.assertIsNotNone(audio_proposal["componentId"])
        self.assertGreater(audio_proposal["relativeConfidence"], 0)
        self.assertGreater(audio_proposal["absoluteConfidence"], 0)
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

    def test_disconnected_audio_component_requires_timestamp_confirmation(self) -> None:
        assets = [
            media("reference-left", "2025-10-15T12:00:00+00:00"),
            media("reference-right", "2025-10-15T12:00:00+00:00"),
            media("later-left", "2025-10-15T12:02:00+00:00"),
            media("later-right", "2025-10-15T12:02:00+00:00"),
        ]
        project = new_project(
            "Disconnected evidence", "library", assets, initialize_legacy_program=False
        )
        base = tuple(pulse_signal())
        result = analyze_project_alignment(
            project,
            {item["id"]: item for item in assets},
            FakeSignatureCache(
                {item["id"]: AudioSignature(item["id"], 400, base, False) for item in assets}
            ),
            overlap_search_extension_us=45_000_000,
        )

        proposals = {item["assetId"]: item for item in result["proposals"]}
        self.assertTrue(proposals["reference-left"]["automaticallyAcceptable"])
        self.assertTrue(proposals["reference-right"]["automaticallyAcceptable"])
        self.assertFalse(proposals["later-left"]["automaticallyAcceptable"])
        self.assertFalse(proposals["later-right"]["automaticallyAcceptable"])
        self.assertIn(
            "unconfirmed timestamp anchor",
            " ".join(proposals["later-left"]["limitations"]),
        )


class EvidenceTimelineTests(unittest.TestCase):
    def test_alignment_digest_changes_with_program_eligibility(self) -> None:
        assets = [media("asset", "2025-10-15T12:00:00+00:00")]
        project = new_project("Digest", "library", assets, initialize_legacy_program=False)
        clip = project["clips"][0]
        clip["alignmentState"] = "ACCEPTED"
        clip["programEligibility"] = "ELIGIBLE"
        eligible_digest = alignment_digest(project)
        clip["programEligibility"] = "EXCLUDED"
        excluded_digest = alignment_digest(project)
        self.assertNotEqual(eligible_digest, excluded_digest)

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
            clip["programEligibility"] = "ELIGIBLE"
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
            clip["programEligibility"] = "ELIGIBLE"
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

    def test_redundant_conflict_warns_while_accepted_eligible_coverage_is_ready(self) -> None:
        project, assets = self.project_with_recorded_gap()
        for clip in project["clips"]:
            clip["alignmentState"] = "ACCEPTED"
            clip["programEligibility"] = "ELIGIBLE"
        conflicting = next(clip for clip in project["clips"] if clip["assetId"] == "b-early")
        conflicting["alignmentState"] = "REVIEW_REQUIRED"
        conflicting["programEligibility"] = "HELD_FOR_REVIEW"
        summary = alignment_summary(project, assets)
        self.assertTrue(summary["readyForProgramDraft"])
        self.assertIn(
            "REDUNDANT_CONFLICTING_CLIP", {item["code"] for item in summary["warnings"]}
        )

    def test_redundant_missing_clip_warns_without_hiding_its_interval(self) -> None:
        project, assets = self.project_with_recorded_gap()
        assets["b-early"]["missing"] = True
        summary = alignment_summary(project, assets)
        self.assertTrue(summary["readyForProgramDraft"])
        self.assertIn(
            "REDUNDANT_UNAVAILABLE_CLIP", {item["code"] for item in summary["warnings"]}
        )

    def test_missing_sole_coverage_blocks_exact_interval(self) -> None:
        project, assets = self.project_with_recorded_gap()
        assets["a-late"]["missing"] = True
        assets["b-late"]["missing"] = True
        summary = alignment_summary(project, assets)
        blocker = next(
            item for item in summary["blockers"] if item["code"] == "SOLE_COVERAGE_UNAVAILABLE"
        )
        self.assertFalse(summary["readyForProgramDraft"])
        self.assertEqual(
            (blocker["startAlignedUs"], blocker["endAlignedUs"]),
            (20_000_000, 30_000_000),
        )

    def test_selected_clip_without_duration_blocks_readiness(self) -> None:
        project, assets = self.project_with_recorded_gap()
        assets["b-early"]["durationUs"] = 0
        assets["b-early"]["duration"] = 0
        summary = alignment_summary(project, assets)
        blocker = next(
            item for item in summary["blockers"] if item["code"] == "DURATION_UNRESOLVED"
        )
        self.assertFalse(summary["readyForProgramDraft"])
        self.assertEqual(
            blocker["clipIds"],
            [next(clip["id"] for clip in project["clips"] if clip["assetId"] == "b-early")],
        )

    def test_unassigned_evidence_between_timeline_sections_blocks_readiness(self) -> None:
        project, assets = self.project_with_recorded_gap()
        project["timelineSections"] = [
            {
                "id": "early-only",
                "startAlignedUs": 0,
                "endAlignedUs": 10_000_000,
                "mode": "KEEP",
                "slateText": None,
            }
        ]
        summary = alignment_summary(project, assets)
        blockers = [
            item for item in summary["blockers"] if item["code"] == "TIMELINE_SECTION_REQUIRED"
        ]
        self.assertFalse(summary["readyForProgramDraft"])
        self.assertEqual(
            [(item["startAlignedUs"], item["endAlignedUs"]) for item in blockers],
            [(20_000_000, 30_000_000)],
        )

    def test_sole_coverage_held_clip_blocks_exact_interval(self) -> None:
        project, assets = self.project_with_recorded_gap()
        for clip in project["clips"]:
            clip["alignmentState"] = "ACCEPTED"
            clip["programEligibility"] = "ELIGIBLE"
        for clip in project["clips"]:
            if clip["assetId"].endswith("late"):
                clip["programEligibility"] = "HELD_FOR_REVIEW"
        summary = alignment_summary(project, assets)
        self.assertFalse(summary["readyForProgramDraft"])
        blocker = next(
            item for item in summary["blockers"] if item["code"] == "SOLE_COVERAGE_TIMING_UNRESOLVED"
        )
        self.assertEqual((blocker["startAlignedUs"], blocker["endAlignedUs"]), (20_000_000, 30_000_000))

    def test_eligibility_requires_accepted_alignment(self) -> None:
        project, assets = self.project_with_recorded_gap()
        clip = project["clips"][0]
        clip["alignmentState"] = "PROVISIONAL"
        clip["programEligibility"] = "HELD_FOR_REVIEW"
        with self.assertRaisesRegex(Exception, "Only accepted clips"):
            apply_command(
                project,
                "SetClipProgramEligibility",
                {"clipIds": [clip["id"]], "programEligibility": "ELIGIBLE"},
                assets,
            )


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
            "algorithmVersion": "2",
            "config": {"overlapSearchExtensionUs": 30_000_000},
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

    def test_timestamp_prior_acceptance_requires_matching_preview(self) -> None:
        (self.source / "timestamp.mp4").write_bytes(b"media")
        record = MediaRecord(
            "timestamp-asset", self.library["id"], "timestamp.mp4", 5, 1,
            duration=5, duration_us=5_000_000,
            captured_at="2025-10-15T12:00:00+00:00", audio_codec="aac",
            fingerprint={"size": 5, "modifiedNs": 1},
        )
        scan = self.store.begin_scan(self.library["id"], "FULL")
        self.store.save_media_batch(scan["id"], [record])
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 1})
        project = self.store.create_project("Timestamp", self.library["id"], ["timestamp-asset"])
        clip = project["clips"][0]
        proposal_set = {
            "id": "timestamp-set", "projectId": project["id"],
            "projectRevision": project["revision"],
            "selectionDigest": project["selectionSnapshot"]["digest"],
            "inputDigest": "1" * 64, "digest": "2" * 64,
            "algorithm": "bounded-audio-evidence-graph", "algorithmVersion": "3",
            "config": {"overlapSearchExtensionUs": 30_000_000},
            "configDigest": "3" * 64, "status": "PENDING", "summary": {},
            "proposals": [{
                "id": "timestamp-proposal", "clipId": clip["id"], "assetId": "timestamp-asset",
                "logicalSourceId": clip["logicalSourceId"], "classification": "TIMESTAMP_ONLY",
                "proposedAlignment": {
                    "anchorSourceUs": 5_000,
                    "anchorAlignedUs": 20_000,
                    "ratePpm": 0,
                },
                "proposedEndAlignedUs": 5_015_000,
                "confidence": 0.55, "automaticallyAcceptable": False,
                "requiresDriftConfirmation": False, "evidence": [], "limitations": [],
                "inputFingerprintDigest": "4" * 64,
            }],
            "limitations": [], "acceptedProposalIds": [], "rejectedProposalIds": [],
            "createdAt": "2025-10-15T12:00:00+00:00", "updatedAt": "2025-10-15T12:00:00+00:00",
        }
        self.store.save_alignment_proposal_set(proposal_set)
        preview = self.store.create_alignment_acceptance_preview(project["id"], {
            "expectedRevision": project["revision"], "proposalSetId": proposal_set["id"],
            "proposalSetDigest": proposal_set["digest"], "mode": "TIMESTAMP_PRIOR",
            "scope": {"kind": "ALIGNED_RANGE", "startAlignedUs": 14_000, "endAlignedUs": 16_000},
        })
        self.assertEqual(preview["mode"], "TIMESTAMP_PRIOR")
        result = self.store.apply_project_command(project["id"], {
            "commandId": "accept-timestamp", "expectedRevision": project["revision"],
            "commandType": "AcceptAlignmentProposalSet", "payload": {
                "proposalSetId": proposal_set["id"], "digest": proposal_set["digest"],
                "mode": "TIMESTAMP_PRIOR", "scope": {
                    "kind": "ALIGNED_RANGE", "startAlignedUs": 14_000, "endAlignedUs": 16_000,
                },
                "previewId": preview["id"], "previewDigest": preview["digest"],
                "confirmTimestampUncertainty": True,
            },
        })
        accepted = result["project"]["clips"][0]
        self.assertEqual(accepted["alignmentState"], "ACCEPTED")
        self.assertEqual(accepted["programEligibility"], "ELIGIBLE")
        self.assertEqual(accepted["alignmentEvidence"], ["timestamp-prior"])
        self.assertEqual(accepted["alignment"]["anchorSourceUs"], 5_000)
        self.assertEqual(accepted["alignment"]["anchorAlignedUs"], 20_000)

    def test_alignment_scope_rejects_malformed_collection_and_range_fields(self) -> None:
        proposal_set = {"proposals": []}
        with self.assertRaisesRegex(DomainError, "clipIds must be an array"):
            self.store._select_alignment_proposals(
                proposal_set, "TIMESTAMP_PRIOR", {"kind": "CLIPS", "clipIds": "clip"}, set()
            )
        with self.assertRaisesRegex(DomainError, "bounds must be integers"):
            self.store._select_alignment_proposals(
                proposal_set,
                "TIMESTAMP_PRIOR",
                {"kind": "ALIGNED_RANGE", "startAlignedUs": "soon", "endAlignedUs": 5},
                set(),
            )


if __name__ == "__main__":
    unittest.main()
