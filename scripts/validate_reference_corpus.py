from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from room_alignment.scanner import iter_scan_records
from room_alignment.store import Store


def source_snapshot(root: Path) -> dict[str, int | str]:
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode())
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode())
        digest.update(b"\n")
        files += 1
        total_bytes += stat.st_size
    return {"treeDigest": digest.hexdigest(), "files": files, "bytes": total_bytes}


def validate(root: Path, state_dir: Path, probe_workers: int) -> dict[str, object]:
    source = root.resolve(strict=True)
    state = state_dir.resolve()
    if state == source or state.is_relative_to(source) or source.is_relative_to(state):
        raise ValueError("Validation state directory must not overlap source corpus")
    before = source_snapshot(source)
    state.mkdir(parents=True, exist_ok=True)
    store = Store(state / "room-alignment.sqlite3")
    grant = store.create_grant(source, "READ_ONLY_SOURCE")
    library = store.create_library(grant["id"], "UTC")
    scan = store.begin_scan(library["id"], "FULL")
    started = time.monotonic()
    first_progress_seconds: float | None = None
    batch = []
    warnings = 0
    tracemalloc.start()
    for record in iter_scan_records(source, library["id"], mode="FULL", probe_workers=probe_workers):
        if first_progress_seconds is None:
            first_progress_seconds = time.monotonic() - started
        batch.append(record)
        warnings += int(bool(record.warning))
        store.scan_progress(scan["id"], warning=bool(record.warning), message="Validating corpus")
        if len(batch) >= 50:
            store.save_media_batch(scan["id"], batch)
            batch.clear()
    if batch:
        store.save_media_batch(scan["id"], batch)
    store.finish_scan(scan["id"], "SUCCEEDED", {"validation": True})
    final_scan = store.scan(scan["id"])
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    after = source_snapshot(source)
    if before != after:
        raise RuntimeError("Source corpus tree metadata changed during read-only validation")
    return {
        "sourceTreeMetadataPreserved": True,
        "sourceTreeMetadataDigest": after["treeDigest"],
        "sourceFiles": after["files"],
        "sourceBytes": after["bytes"],
        "indexedVideos": final_scan["videos"],
        "warnings": warnings,
        "firstProgressSeconds": round(first_progress_seconds or 0, 3),
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "peakPythonBytes": peak,
        "probeWorkers": probe_workers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only, sanitized reference-corpus conformance scan")
    parser.add_argument("root", type=Path)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--probe-workers", type=int, default=4, choices=range(1, 9))
    args = parser.parse_args()
    print(json.dumps(validate(args.root, args.state_dir, args.probe_workers), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
