from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import MediaRecord, ProvenanceEvidence, ScanSummary
from .provenance import infer_from_path, merge_evidence, read_sidecar


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".m2ts", ".ts"}


def stable_media_id(library_id: str, relative_path: str) -> str:
    return hashlib.sha256(f"{library_id}\0{relative_path}".encode()).hexdigest()[:24]


def iter_videos(root: Path) -> Iterable[Path]:
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not name.startswith(".")]
        for filename in files:
            path = Path(directory, filename)
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                yield path


def _rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None


def probe(path: Path, timeout: float = 15) -> tuple[dict[str, Any], list[ProvenanceEvidence], str | None]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,start_time,format_name:format_tags=creation_time,date,location,com.apple.quicktime.creationdate:stream=index,codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels:stream_tags=creation_time,handler_name",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {}, [], f"ffprobe unavailable or timed out: {error}"
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown ffprobe error"
        return {}, [], detail[:300]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}, [], "ffprobe returned invalid JSON"
    values: dict[str, Any] = {}
    evidence: list[ProvenanceEvidence] = []
    fmt = payload.get("format", {})
    try:
        values["duration"] = float(fmt["duration"])
    except (KeyError, TypeError, ValueError):
        pass
    tags = fmt.get("tags", {}) if isinstance(fmt.get("tags"), dict) else {}
    captured = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate") or tags.get("date")
    if captured:
        values["captured_at"] = captured
        evidence.append(ProvenanceEvidence("container", "captured_at", captured, 0.85, "format.tags"))
    if tags:
        values["custom"] = {f"format_tag.{key}": value for key, value in tags.items()}
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "video" and "video_codec" not in values:
            values.update({
                "video_codec": stream.get("codec_name"), "width": stream.get("width"),
                "height": stream.get("height"), "frame_rate": _rate(stream.get("avg_frame_rate")),
            })
        elif stream.get("codec_type") == "audio" and "audio_codec" not in values:
            values.update({
                "audio_codec": stream.get("codec_name"),
                "sample_rate": int(stream["sample_rate"]) if str(stream.get("sample_rate", "")).isdigit() else None,
                "channels": stream.get("channels"),
            })
    return values, evidence, None


def scan_library(
    root: Path,
    library_id: str,
    on_record: Callable[[MediaRecord], None] | None = None,
    max_files: int | None = None,
) -> tuple[ScanSummary, list[MediaRecord]]:
    resolved = root.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Library root must be a directory")
    records: list[MediaRecord] = []
    warnings = 0
    cameras: set[str] = set()
    dates: dict[str, int] = {}
    scanned = 0
    for path in iter_videos(resolved):
        if max_files is not None and scanned >= max_files:
            break
        scanned += 1
        relative = path.relative_to(resolved)
        stat = path.stat()
        inferred = infer_from_path(path, relative)
        sidecar = read_sidecar(path, relative)
        probed_values, probed_evidence, warning = probe(path)
        values, evidence = merge_evidence(inferred, (probed_values, probed_evidence), sidecar)
        record = MediaRecord(
            id=stable_media_id(library_id, relative.as_posix()), library_id=library_id,
            relative_path=relative.as_posix(), size=stat.st_size, modified_ns=stat.st_mtime_ns,
            warning=warning, evidence=evidence, **values,
        )
        if warning:
            warnings += 1
        if record.camera:
            cameras.add(record.camera)
        if record.captured_at:
            date = str(record.captured_at)[:10]
            dates[date] = dates.get(date, 0) + 1
        records.append(record)
        if on_record:
            on_record(record)
    summary = ScanSummary(library_id, str(resolved), scanned, len(records), warnings, sorted(cameras), dates)
    return summary, records

