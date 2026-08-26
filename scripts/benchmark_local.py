from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from room_alignment.models import MediaRecord
from room_alignment.store import Store


ASSET_COUNT = 26_520
BLOCK_COUNT = 1_000


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))]


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="room-alignment-benchmark-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        store = Store(root / "state.sqlite3")
        grant = store.create_grant(source, "READ_ONLY_SOURCE")
        library = store.create_library(grant["id"], "UTC")
        scan = store.begin_scan(library["id"], "FULL")
        tracemalloc.start()
        started = time.perf_counter()
        for batch_start in range(0, ASSET_COUNT, 250):
            batch = [
                MediaRecord(
                    id=f"asset-{index:06d}", library_id=library["id"], relative_path=f"synthetic/{index:06d}.mp4",
                    size=1_000, modified_ns=1, duration=1_000, duration_us=1_000_000_000,
                    streams=[{"id": f"video-{index:06d}", "codecType": "video"}, {"id": f"audio-{index:06d}", "codecType": "audio"}],
                    fingerprint={"size": 1_000, "modifiedNs": 1}, source_candidate_id=f"candidate-{index:06d}",
                )
                for index in range(batch_start, min(ASSET_COUNT, batch_start + 250))
            ]
            store.save_media_batch(scan["id"], batch)
        store.finish_scan(scan["id"], "SUCCEEDED", {"videos": ASSET_COUNT})
        index_seconds = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        read_times = []
        cursor = None
        for _page_number in range(50):
            page_started = time.perf_counter()
            page = store.media_page(library["id"], 200, cursor, scan["generation"])
            read_times.append((time.perf_counter() - page_started) * 1000)
            cursor = page["nextCursor"]
            if cursor is None:
                break

        project = store.create_project("Scale", library["id"], ["asset-000000"])
        source_id = project["logicalSources"][0]["id"]
        project["videoBlocks"] = [
            {"id": f"v-{index}", "startUs": index * 1_000_000, "endUs": (index + 1) * 1_000_000, "logicalSourceId": source_id, "pinnedClipId": project["clips"][0]["id"]}
            for index in range(BLOCK_COUNT)
        ]
        project["audioBlocks"] = [{"id": "a", "startUs": 0, "endUs": BLOCK_COUNT * 1_000_000, "mode": "FOLLOW_VIDEO", "logicalSourceId": None, "clipId": None, "offsetUs": 0, "ratePpm": 0}]
        store.save_project(project)
        command_times = []
        for index in range(20):
            command_started = time.perf_counter()
            result = store.apply_project_command(project["id"], {"commandId": f"benchmark-{index}", "expectedRevision": project["revision"], "commandType": "UpdateProjectMetadata", "payload": {"name": f"Scale {index}"}})
            project = result["project"]
            command_times.append((time.perf_counter() - command_started) * 1000)

        replay_started = time.perf_counter()
        events = store.events(0, 1_000)
        replay_ms = (time.perf_counter() - replay_started) * 1000
        return {
            "assets": ASSET_COUNT, "blocks": BLOCK_COUNT, "indexSeconds": round(index_seconds, 3),
            "peakPythonBytes": peak, "readP95Ms": round(percentile(read_times, 0.95), 3),
            "commandP95Ms": round(percentile(command_times, 0.95), 3), "commandMedianMs": round(statistics.median(command_times), 3),
            "eventReplayMs": round(replay_ms, 3), "eventCount": len(events),
        }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
