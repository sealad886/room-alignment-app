from __future__ import annotations

import unittest

from room_alignment.domain import (
    DomainError,
    SyncTransform,
    apply_command,
    compile_program,
    new_project,
    program_at,
    seconds_to_us,
)


def asset(asset_id: str, camera: str, duration_us: int, audio: bool = True) -> dict:
    return {
        "id": asset_id,
        "camera": camera,
        "durationUs": duration_us,
        "audio_codec": "aac" if audio else None,
        "streams": [
            {"id": f"{asset_id}-video", "codecType": "video"},
            *([{"id": f"{asset_id}-audio", "codecType": "audio"}] if audio else []),
        ],
    }


class TimeTransformTests(unittest.TestCase):
    def test_seconds_use_deterministic_half_even_microseconds(self):
        self.assertEqual(seconds_to_us("1.2345675"), 1_234_568)
        self.assertEqual(seconds_to_us("1.2345665"), 1_234_566)

    def test_affine_transform_round_trips_within_one_microsecond(self):
        transform = SyncTransform(anchor_source_us=250_000, anchor_output_us=900_000, rate_ppm=731)
        for source_us in (-5_000_000, 0, 250_000, 90_000_000):
            round_trip = transform.output_to_source(transform.source_to_output(source_us))
            self.assertLessEqual(abs(round_trip - source_us), 1)

    def test_rate_bounds_reject_instead_of_clamp(self):
        with self.assertRaisesRegex(DomainError, "ratePpm"):
            SyncTransform(rate_ppm=2_001)


class ProgramCompilerTests(unittest.TestCase):
    def test_empty_video_program_is_blocking(self):
        media = asset("a", "Door", 5_000_000)
        project = new_project("Event", "lib", [media], "project")
        project["videoBlocks"] = []
        project["audioBlocks"] = []
        compiled = compile_program(project, {"a": media})
        self.assertFalse(compiled["valid"])
        self.assertIn("VIDEO_GAP", {item["code"] for item in compiled["issues"]})

    def test_one_logical_source_block_compiles_across_consecutive_clips(self):
        assets = [asset("a", "Door", 5_000_000), asset("b", "Door", 5_000_000)]
        project = new_project("Event", "lib", assets, "project")
        source_id = project["logicalSources"][0]["id"]
        project["clips"][1]["logicalSourceId"] = source_id
        project["clips"][1]["sync"]["anchorOutputUs"] = 5_000_000
        project["videoBlocks"] = [
            {
                "id": "video",
                "startUs": 0,
                "endUs": 10_000_000,
                "logicalSourceId": source_id,
                "pinnedClipId": None,
            }
        ]
        project["audioBlocks"] = [
            {
                "id": "audio",
                "startUs": 0,
                "endUs": 10_000_000,
                "mode": "FOLLOW_VIDEO",
                "logicalSourceId": None,
                "clipId": None,
                "offsetUs": 0,
                "ratePpm": 0,
            }
        ]
        compiled = compile_program(project, {item["id"]: item for item in assets})
        self.assertTrue(compiled["valid"], compiled["issues"])
        self.assertEqual([item["assetId"] for item in compiled["videoSlices"]], ["a", "b"])

    def test_ambiguous_overlapping_clips_block_until_clip_is_pinned(self):
        assets = [asset("a", "Door", 10_000_000), asset("b", "Door", 10_000_000)]
        project = new_project("Event", "lib", assets, "project")
        source_id = project["logicalSources"][0]["id"]
        project["clips"][1]["logicalSourceId"] = source_id
        project["videoBlocks"] = [
            {
                "id": "video",
                "startUs": 0,
                "endUs": 10_000_000,
                "logicalSourceId": source_id,
                "pinnedClipId": None,
            }
        ]
        project["audioBlocks"] = [
            {
                "id": "audio",
                "startUs": 0,
                "endUs": 10_000_000,
                "mode": "SILENCE",
                "logicalSourceId": None,
                "clipId": None,
                "offsetUs": 0,
                "ratePpm": 0,
            }
        ]
        by_id = {item["id"]: item for item in assets}
        ambiguous = compile_program(project, by_id)
        self.assertIn("AMBIGUOUS", {item["code"] for item in ambiguous["issues"]})
        project["videoBlocks"][0]["pinnedClipId"] = project["clips"][0]["id"]
        resolved = compile_program(project, by_id)
        self.assertTrue(resolved["valid"], resolved["issues"])

    def test_source_candidates_never_silently_become_one_logical_source(self):
        assets = [asset("a", "Same label", 5_000_000), asset("b", "Same label", 5_000_000)]
        assets[0]["sourceCandidateId"] = assets[1]["sourceCandidateId"] = "candidate-same"
        project = new_project("Event", "lib", assets, "project")
        self.assertEqual(len(project["logicalSources"]), 2)
        self.assertNotEqual(project["clips"][0]["logicalSourceId"], project["clips"][1]["logicalSourceId"])

    def test_follow_video_without_audio_blocks_instead_of_inventing_silence(self):
        media = asset("a", "Door", 5_000_000, audio=False)
        project = new_project("Event", "lib", [media], "project")
        compiled = compile_program(project, {"a": media})
        self.assertIn("AUDIO_UNAVAILABLE", {item["code"] for item in compiled["issues"]})
        self.assertFalse(compiled["audioSlices"])

    def test_explicit_silence_is_independent_from_video(self):
        media = asset("a", "Door", 5_000_000, audio=False)
        project = new_project("Event", "lib", [media], "project")
        project["audioBlocks"][0]["mode"] = "SILENCE"
        compiled = compile_program(project, {"a": media})
        self.assertTrue(compiled["valid"], compiled["issues"])
        self.assertTrue(compiled["audioSlices"][0]["synthetic"])
        self.assertEqual(program_at(compiled, 1_000_000)["audio"]["mode"], "SILENCE")

    def test_independent_audio_offset_and_rate_compile_to_exact_source_range(self):
        media = asset("a", "Door", 5_000_000)
        project = new_project("Event", "lib", [media], "project")
        project["audioBlocks"][0].update(offsetUs=1_000, ratePpm=1_000)
        compiled = compile_program(project, {"a": media})
        audio = compiled["audioSlices"][0]
        expected_duration = SyncTransform(rate_ppm=1_000).output_to_source(5_000_000)
        self.assertEqual(audio["sourceStartUs"], 1_000)
        self.assertEqual(audio["sourceEndUs"], expected_duration + 1_000)
        self.assertIn("audio rate correction 1000 ppm", audio["transforms"])


class CommandTests(unittest.TestCase):
    def test_malformed_payload_is_a_stable_validation_error(self):
        media = asset("a", "Door", 5_000_000)
        project = new_project("Event", "lib", [media], "project")
        with self.assertRaises(DomainError) as raised:
            apply_command(project, "RenameLogicalSource", {}, {"a": media})
        self.assertEqual(raised.exception.code, "VALIDATION_FAILED")
        with self.assertRaises(DomainError) as unknown:
            apply_command(
                project,
                "UpdateProjectMetadata",
                {"name": "Okay", "future": True},
                {"a": media},
            )
        self.assertEqual(unknown.exception.code, "VALIDATION_FAILED")

    def test_merge_preserves_reference_source_and_cut_uses_requested_block_id(self):
        assets = [asset("a", "Door", 5_000_000), asset("b", "Yard", 5_000_000)]
        project = new_project("Event", "lib", assets, "project")
        target, merged = project["logicalSources"]
        target["reference"] = False
        merged["reference"] = True
        project = apply_command(
            project,
            "MergeLogicalSources",
            {"targetSourceId": target["id"], "sourceIds": [merged["id"]]},
            {item["id"]: item for item in assets},
        )
        self.assertTrue(project["logicalSources"][0]["reference"])
        project = apply_command(
            project,
            "CutToSource",
            {
                "blockId": project["videoBlocks"][0]["id"],
                "atUs": 2_500_000,
                "logicalSourceId": target["id"],
                "newBlockId": "known-cut",
            },
            {item["id"]: item for item in assets},
        )
        cut = next(item for item in project["videoBlocks"] if item["id"] == "known-cut")
        self.assertEqual(cut["startUs"], 2_500_000)
    def test_program_time_sync_change_keeps_video_boundaries(self):
        media = asset("a", "Door", 5_000_000)
        project = new_project("Event", "lib", [media], "project")
        before = [dict(item) for item in project["videoBlocks"]]
        changed = apply_command(
            project,
            "SetSyncTransform",
            {
                "clipId": project["clips"][0]["id"],
                "sync": {"anchorSourceUs": 0, "anchorOutputUs": 500_000, "ratePpm": 0},
            },
            {"a": media},
        )
        self.assertEqual(
            [(item["startUs"], item["endUs"]) for item in changed["videoBlocks"]],
            [(item["startUs"], item["endUs"]) for item in before],
        )

    def test_source_time_sync_change_preserves_source_points_and_moves_boundaries(self):
        media = asset("a", "Door", 5_000_000)
        project = new_project("Event", "lib", [media], "project")
        project["anchorMode"] = "SOURCE_TIME"
        changed = apply_command(
            project,
            "SetSyncTransform",
            {
                "clipId": project["clips"][0]["id"],
                "sync": {"anchorSourceUs": 0, "anchorOutputUs": 500_000, "ratePpm": 0},
            },
            {"a": media},
        )
        self.assertEqual(changed["videoBlocks"][0]["startUs"], 500_000)
        self.assertEqual(changed["videoBlocks"][0]["endUs"], 5_500_000)

    def test_nonzero_drift_requires_explicit_confirmation(self):
        media = asset("a", "Door", 5_000_000)
        project = new_project("Event", "lib", [media], "project")
        with self.assertRaisesRegex(DomainError, "confirmDrift"):
            apply_command(
                project,
                "SetSyncTransform",
                {
                    "clipId": project["clips"][0]["id"],
                    "sync": {"anchorSourceUs": 0, "anchorOutputUs": 0, "ratePpm": 100},
                },
                {"a": media},
            )


if __name__ == "__main__":
    unittest.main()
