from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from room_alignment.domain import DomainError
from room_alignment.models import MediaRecord
from room_alignment.store import Store


class HierarchicalClusteringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        grant = self.store.create_grant(self.root, "READ_ONLY_SOURCE")
        self.library = self.store.create_library(grant["id"], "UTC")

    def record(
        self,
        asset_id: str,
        captured_at: str | None,
        duration_us: int,
        source_candidate_id: str,
    ) -> MediaRecord:
        relative_path = f"{asset_id}.mp4"
        (self.root / relative_path).write_bytes(asset_id.encode())
        return MediaRecord(
            asset_id,
            self.library["id"],
            relative_path,
            len(asset_id),
            1,
            duration=duration_us / 1_000_000,
            duration_us=duration_us,
            captured_at=captured_at,
            camera="Same display label",
            source_candidate_id=source_candidate_id,
            video_codec="h264",
            fingerprint={"size": len(asset_id), "modifiedNs": 1},
        )

    def scan(self, records: list[MediaRecord]) -> int:
        scan = self.store.begin_scan(self.library["id"], "FULL")
        self.store.save_media_batch(scan["id"], records)
        self.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": len(records)})
        return self.store.library(self.library["id"])["catalogRevision"]

    def build(self, catalog_revision: int) -> dict[str, object]:
        job = self.store.begin_cluster_generation(
            self.library["id"], catalog_revision, 15_000_000, 120_000_000
        )
        return self.store.build_cluster_generation(str(job["clusterGenerationId"]))

    def test_running_coverage_end_builds_events_inside_sessions(self) -> None:
        revision = self.scan(
            [
                self.record("a", "2025-10-15T00:00:00+00:00", 30_000_000, "front"),
                self.record("b", "2025-10-15T00:00:40+00:00", 10_000_000, "rear"),
                self.record("c", "2025-10-15T00:01:10+00:00", 5_000_000, "front"),
                self.record("d", "2025-10-15T00:03:30+00:00", 5_000_000, "front"),
                self.record("unknown", None, 5_000_000, "rear"),
            ]
        )

        generation = self.build(revision)

        self.assertEqual(generation["status"], "SUCCEEDED")
        self.assertEqual(generation["sessionCount"], 2)
        self.assertEqual(generation["eventCount"], 3)
        self.assertEqual(generation["clusteredAssetCount"], 4)
        self.assertEqual(generation["unclusteredAssetCount"], 1)
        sessions = self.store.cluster_summaries_page(str(generation["id"]), "SESSION")
        self.assertEqual([item["eventCount"] for item in sessions["items"]], [2, 1])
        self.assertEqual([item["clipCount"] for item in sessions["items"]], [3, 1])
        first_events = self.store.cluster_summaries_page(
            str(generation["id"]), "EVENT", session_id=sessions["items"][0]["id"]
        )
        self.assertEqual([item["clipCount"] for item in first_events["items"]], [2, 1])
        first_members = self.store.cluster_memberships_page(
            first_events["items"][0]["id"], "EVENT"
        )
        self.assertEqual([item["assetId"] for item in first_members["items"]], ["a", "b"])
        facets = self.store.cluster_facets(str(generation["id"]))
        self.assertEqual({item["id"] for item in facets["sourceCandidates"]}, {"front", "rear"})
        rear_sessions = self.store.cluster_summaries_page(
            str(generation["id"]), "SESSION", source_candidate_id="rear"
        )
        self.assertEqual([item["id"] for item in rear_sessions["items"]], [sessions["items"][0]["id"]])
        unknown = self.store.unclustered_memberships_page(str(generation["id"]))
        self.assertEqual([item["assetId"] for item in unknown["items"]], ["unknown"])

    def test_generation_is_immutable_and_catalog_bound(self) -> None:
        revision = self.scan(
            [self.record("a", "2025-10-15T00:00:00+00:00", 5_000_000, "front")]
        )
        generation = self.build(revision)
        first_page = self.store.cluster_generations_page(self.library["id"], limit=1)
        self.assertEqual(first_page["items"][0]["config"], {
            "eventGapUs": 15_000_000,
            "sessionGapUs": 120_000_000,
        })

        newer_revision = self.scan(
            [
                self.record("a", "2025-10-15T00:00:00+00:00", 5_000_000, "front"),
                self.record("b", "2025-10-15T00:00:10+00:00", 5_000_000, "rear"),
            ]
        )
        self.assertGreater(newer_revision, revision)
        with self.assertRaisesRegex(DomainError, "Catalog changed"):
            self.store.begin_cluster_generation(
                self.library["id"], revision, 15_000_000, 120_000_000
            )
        retained = self.store.cluster_generation(str(generation["id"]))
        self.assertEqual(retained["catalogRevision"], revision)
        self.assertEqual(retained["clusteredAssetCount"], 1)

    def test_timestamp_policy_change_advances_catalog_without_mutating_generation(self) -> None:
        revision = self.scan(
            [self.record("a", "2025-10-15T00:00:00+00:00", 5_000_000, "front")]
        )
        generation = self.build(revision)
        changed = self.store.update_library_time_policy(
            self.library["id"], "Europe/Paris", 0, "REJECT"
        )
        self.assertGreater(changed["catalogRevision"], revision)
        self.assertEqual(
            self.store.cluster_generation(str(generation["id"]))["status"], "SUCCEEDED"
        )
        with self.assertRaisesRegex(DomainError, "Catalog changed"):
            self.store.begin_cluster_generation(
                self.library["id"], revision, 15_000_000, 120_000_000
            )

    def test_summary_and_membership_pagination_are_stable(self) -> None:
        revision = self.scan(
            [
                self.record(
                    f"asset-{index}",
                    f"2025-10-15T00:{index * 3:02d}:00+00:00",
                    5_000_000,
                    "front",
                )
                for index in range(4)
            ]
        )
        generation = self.build(revision)
        first = self.store.cluster_summaries_page(str(generation["id"]), "SESSION", limit=2)
        second = self.store.cluster_summaries_page(
            str(generation["id"]), "SESSION", limit=2, cursor=first["nextCursor"]
        )
        ids = [item["id"] for item in first["items"] + second["items"]]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        self.assertIsNone(second["nextCursor"])
        for cursor in ("%%%", "W10"):
            with self.assertRaisesRegex(DomainError, "Invalid pagination cursor"):
                self.store.cluster_summaries_page(
                    str(generation["id"]), "SESSION", cursor=cursor
                )

    def test_generation_streams_across_persistence_batches(self) -> None:
        revision = self.scan(
            [
                self.record(
                    f"asset-{index:04d}",
                    "2025-10-15T00:00:00+00:00",
                    5_000_000,
                    "front",
                )
                for index in range(1_005)
            ]
        )

        generation = self.build(revision)

        self.assertEqual(generation["status"], "SUCCEEDED")
        self.assertEqual(generation["clusteredAssetCount"], 1_005)
        self.assertEqual(generation["sessionCount"], 1)
        self.assertEqual(generation["eventCount"], 1)
        sessions = self.store.cluster_summaries_page(str(generation["id"]), "SESSION")
        self.assertEqual(sessions["items"][0]["clipCount"], 1_005)

    def test_multi_cluster_project_selection_is_an_exact_immutable_snapshot(self) -> None:
        revision = self.scan(
            [
                self.record("a", "2025-10-15T00:00:00+00:00", 5_000_000, "front"),
                self.record("b", "2025-10-15T00:00:10+00:00", 5_000_000, "front"),
                self.record("c", "2025-10-15T00:03:00+00:00", 5_000_000, "rear"),
                self.record("manual", None, 5_000_000, "rear"),
            ]
        )
        generation = self.build(revision)
        sessions = self.store.cluster_summaries_page(str(generation["id"]), "SESSION")[
            "items"
        ]
        first_events = self.store.cluster_summaries_page(
            str(generation["id"]), "EVENT", session_id=sessions[0]["id"]
        )["items"]

        project = self.store.create_project_from_selection(
            "Several events",
            self.library["id"],
            str(generation["id"]),
            [sessions[1]["id"]],
            [first_events[0]["id"]],
            ["manual"],
            ["b"],
        )

        snapshot = project["selectionSnapshot"]
        self.assertEqual(snapshot["selectedSessionIds"], [sessions[1]["id"]])
        self.assertEqual(snapshot["selectedEventIds"], [first_events[0]["id"]])
        self.assertEqual(snapshot["assetIds"], ["a", "c", "manual"])
        self.assertEqual(project["videoBlocks"], [])
        self.assertEqual(project["audioBlocks"], [])
        self.assertEqual(len(project["logicalSources"]), 3)
        self.assertEqual(
            {source["identityState"] for source in project["logicalSources"]},
            {"USER_REVIEW_REQUIRED"},
        )
        original_digest = snapshot["digest"]

        self.scan([self.record("new", "2025-10-16T00:00:00+00:00", 5_000_000, "front")])
        reopened = self.store.project(project["id"])
        self.assertEqual(reopened["selectionSnapshot"]["assetIds"], ["a", "c", "manual"])
        self.assertEqual(reopened["selectionSnapshot"]["digest"], original_digest)

    def test_project_selection_rejects_foreign_cluster_ids(self) -> None:
        revision = self.scan(
            [self.record("a", "2025-10-15T00:00:00+00:00", 5_000_000, "front")]
        )
        generation = self.build(revision)
        with self.assertRaisesRegex(DomainError, "belong to the generation"):
            self.store.create_project_from_selection(
                "Invalid selection",
                self.library["id"],
                str(generation["id"]),
                ["session_elsewhere"],
                [],
                [],
                [],
            )


if __name__ == "__main__":
    unittest.main()
