from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable, Iterable

from .domain import digest_json, seconds_to_us
from .models import MediaRecord, ProvenanceEvidence, ScanSummary
from .provenance import infer_from_path, merge_evidence, read_sidecar


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".m2ts", ".ts"}


def stable_media_id(library_id: str, relative_path: str) -> str:
    return hashlib.sha256(f"{library_id}\0{relative_path}".encode()).hexdigest()[:24]


def quick_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    sidecars = [path.with_suffix(path.suffix + ".json"), path.with_suffix(".json")]
    sidecar = next((item for item in sidecars if item.is_file()), None)
    sidecar_stat = sidecar.stat() if sidecar else None
    with path.open("rb") as handle:
        head = handle.read(64 * 1024)
        if stat.st_size > 64 * 1024:
            handle.seek(max(0, stat.st_size - 64 * 1024))
            tail = handle.read(64 * 1024)
        else:
            tail = b""
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "modifiedNs": stat.st_mtime_ns,
        "sampleSha256": hashlib.sha256(head + tail).hexdigest(),
        "sidecar": (
            {
                "name": sidecar.name,
                "size": sidecar_stat.st_size,
                "modifiedNs": sidecar_stat.st_mtime_ns,
            }
            if sidecar and sidecar_stat
            else None
        ),
        "probeVersion": 2,
    }


def cheap_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    sidecars = [path.with_suffix(path.suffix + ".json"), path.with_suffix(".json")]
    sidecar = next((item for item in sidecars if item.is_file()), None)
    sidecar_stat = sidecar.stat() if sidecar else None
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "modifiedNs": stat.st_mtime_ns,
        "sidecar": (
            {
                "name": sidecar.name,
                "size": sidecar_stat.st_size,
                "modifiedNs": sidecar_stat.st_mtime_ns,
            }
            if sidecar and sidecar_stat
            else None
        ),
        "probeVersion": 2,
    }


def full_digest(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


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
        "format=duration,start_time,format_name:format_tags=creation_time,date,location,com.apple.quicktime.creationdate:"
        "stream=index,codec_type,codec_name,time_base,start_pts,start_time,duration_ts,duration,width,height,"
        "avg_frame_rate,r_frame_rate,sample_aspect_ratio,pix_fmt,color_space,color_transfer,color_primaries,"
        "field_order,sample_rate,channels,channel_layout:stream_tags=creation_time,handler_name,rotate:stream_side_data=rotation",
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
        values["duration_us"] = seconds_to_us(fmt["duration"])
    except (KeyError, TypeError, ValueError):
        pass
    tags = fmt.get("tags", {}) if isinstance(fmt.get("tags"), dict) else {}
    captured = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate") or tags.get("date")
    if captured:
        values["captured_at"] = captured
        evidence.append(ProvenanceEvidence("container", "captured_at", captured, 0.85, "format.tags"))
    if tags:
        values["custom"] = {f"format_tag.{key}": value for key, value in tags.items()}
    streams: list[dict[str, Any]] = []
    for stream in payload.get("streams", []):
        stream_value = {
            "id": f"stream_{stream.get('index', len(streams))}",
            "index": stream.get("index"),
            "codecType": stream.get("codec_type"),
            "codecName": stream.get("codec_name"),
            "timeBase": stream.get("time_base"),
            "startPts": stream.get("start_pts"),
            "startTime": stream.get("start_time"),
            "durationTs": stream.get("duration_ts"),
            "duration": stream.get("duration"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "averageFrameRate": stream.get("avg_frame_rate"),
            "realFrameRate": stream.get("r_frame_rate"),
            "sampleAspectRatio": stream.get("sample_aspect_ratio"),
            "pixelFormat": stream.get("pix_fmt"),
            "colorSpace": stream.get("color_space"),
            "colorTransfer": stream.get("color_transfer"),
            "colorPrimaries": stream.get("color_primaries"),
            "fieldOrder": stream.get("field_order"),
            "sampleRate": stream.get("sample_rate"),
            "channels": stream.get("channels"),
            "channelLayout": stream.get("channel_layout"),
            "rotation": next(
                (
                    item.get("rotation")
                    for item in stream.get("side_data_list", [])
                    if item.get("rotation") is not None
                ),
                (stream.get("tags") or {}).get("rotate"),
            ),
        }
        streams.append({key: value for key, value in stream_value.items() if value is not None})
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
    values["streams"] = streams
    return values, evidence, None


def iter_scan_records(
    root: Path,
    library_id: str,
    *,
    mode: str = "FULL",
    existing_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    canceled: Callable[[], bool] | None = None,
    max_files: int | None = None,
) -> Iterable[MediaRecord]:
    resolved = root.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Library root must be a directory")
    scanned = 0
    for path in iter_videos(resolved):
        if canceled and canceled():
            return
        if max_files is not None and scanned >= max_files:
            return
        scanned += 1
        relative = path.relative_to(resolved)
        relative_text = relative.as_posix()
        cheap = cheap_fingerprint(path)
        existing = existing_lookup(relative_text) if existing_lookup else None
        if mode == "INCREMENTAL" and existing and _fingerprint_matches(existing.get("fingerprint", {}), cheap):
            yield media_record_from_dict(existing, library_id)
            continue
        fingerprint = quick_fingerprint(path)
        inferred = infer_from_path(path, relative)
        sidecar = read_sidecar(path, relative)
        probed_values, probed_evidence, warning = probe(path)
        values, evidence = merge_evidence(inferred, (probed_values, probed_evidence), sidecar)
        candidate_material = {
            "cameraEvidence": [
                {"kind": item.kind, "value": item.value, "origin": item.origin}
                for item in evidence
                if item.field == "camera"
            ],
            "parent": relative.parent.as_posix(),
        }
        yield MediaRecord(
            id=stable_media_id(library_id, relative_text),
            library_id=library_id,
            relative_path=relative_text,
            size=path.stat().st_size,
            modified_ns=path.stat().st_mtime_ns,
            warning=warning,
            evidence=evidence,
            fingerprint=fingerprint,
            source_candidate_id=f"candidate_{digest_json(candidate_material)[:24]}",
            **values,
        )


def _fingerprint_matches(existing: dict[str, Any], current: dict[str, Any]) -> bool:
    keys = {"device", "inode", "size", "modifiedNs", "sidecar", "probeVersion"}
    return all(existing.get(key) == current.get(key) for key in keys)


def media_record_from_dict(value: dict[str, Any], library_id: str | None = None) -> MediaRecord:
    data = dict(value)
    data["library_id"] = library_id or data.pop("libraryId", data.get("library_id"))
    data["duration_us"] = data.pop("durationUs", data.get("duration_us"))
    data["source_candidate_id"] = data.pop("sourceCandidateId", data.get("source_candidate_id"))
    evidence = []
    for item in data.get("evidence", []):
        evidence.append(item if isinstance(item, ProvenanceEvidence) else ProvenanceEvidence(**_evidence_kwargs(item)))
    data["evidence"] = evidence
    allowed = {item.name for item in fields(MediaRecord)}
    return MediaRecord(**{key: value for key, value in data.items() if key in allowed})


def _evidence_kwargs(value: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "rawValue": "raw_value",
        "normalizedValue": "normalized_value",
        "observedAt": "observed_at",
        "extractorVersion": "extractor_version",
    }
    return {aliases.get(key, key): item for key, item in value.items()}


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
    for record in iter_scan_records(resolved, library_id, max_files=max_files):
        scanned += 1
        if record.warning:
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
