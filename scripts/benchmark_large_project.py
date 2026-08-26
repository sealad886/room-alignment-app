from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from room_alignment.domain import (
    compile_program,
    generate_program_draft,
    new_project,
    timeline_section_proposal,
)


def asset(index: int, start: datetime) -> dict[str, object]:
    asset_id = f"asset-{index:06d}"
    return {
        "id": asset_id,
        "library_id": "benchmark-library",
        "relative_path": f"synthetic/{asset_id}.mp4",
        "captured_at": (start + timedelta(seconds=index)).isoformat(),
        "durationUs": 1_000_000,
        "video_codec": "h264",
        "audio_codec": "aac",
        "sourceCandidateId": "candidate-benchmark",
        "streams": [
            {"id": f"{asset_id}-video", "codecType": "video"},
            {"id": f"{asset_id}-audio", "codecType": "audio"},
        ],
        "fingerprint": {"sampleSha256": f"{index:064x}", "size": 1_000},
    }


def run(count: int) -> dict[str, object]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    assets_list = [asset(index, start) for index in range(count)]
    assets = {str(item["id"]): item for item in assets_list}
    project = new_project(
        "Large-project benchmark",
        "benchmark-library",
        assets_list,
        "benchmark-project",
        initialize_legacy_program=False,
    )
    for source in project["logicalSources"]:
        source["identityState"] = "USER_CONFIRMED"
    for clip in project["clips"]:
        clip["alignmentState"] = "ACCEPTED"

    started = time.perf_counter()
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
    compiled = compile_program(draft, assets)
    elapsed = time.perf_counter() - started
    peak_bytes = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        peak_bytes *= 1024
    if not compiled["valid"]:
        raise RuntimeError(f"Large project did not compile: {compiled['issues'][:3]}")
    if int(compiled["durationUs"]) != count * 1_000_000:
        raise RuntimeError("Large-project output duration does not match source coverage")
    return {
        "clips": count,
        "programBlocks": len(draft["videoBlocks"]),
        "videoSlices": len(compiled["videoSlices"]),
        "audioSlices": len(compiled["audioSlices"]),
        "durationUs": compiled["durationUs"],
        "elapsedSeconds": round(elapsed, 3),
        "peakProcessBytes": peak_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark canonical 10,000-clip program generation")
    parser.add_argument("--clips", type=int, default=10_000)
    parser.add_argument("--max-seconds", type=float, default=2.0)
    args = parser.parse_args()
    result = run(args.clips)
    print(json.dumps(result, sort_keys=True))
    return 0 if float(result["elapsedSeconds"]) <= args.max_seconds else 1


if __name__ == "__main__":
    raise SystemExit(main())
