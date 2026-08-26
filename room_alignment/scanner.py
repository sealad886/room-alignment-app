from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import fields
from pathlib import Path
from typing import Any

from .domain import digest_json, seconds_to_us
from .models import MediaRecord, ProvenanceEvidence, ScanSummary
from .provenance import infer_from_path, merge_evidence, read_sidecar


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm", ".mts", ".m2ts", ".ts"}


def stable_media_id(library_id: str, relative_path: str, root_id: str | None = None) -> str:
    return hashlib.sha256(f"{library_id}\0{root_id or 'legacy'}\0{relative_path}".encode()).hexdigest()[:24]


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
        "probeVersion": 3,
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
        "probeVersion": 3,
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
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_relative_to(root) or not resolved.is_file():
                if path.is_symlink():
                    yield path
                continue
            if _has_media_container_signature(resolved):
                yield path


def _has_media_container_signature(path: Path) -> bool:
    """Admit extensionless/unknown-name media without probing arbitrary documents."""
    try:
        with path.open("rb") as handle:
            head = handle.read(400)
    except OSError:
        return False
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return True
    if head.startswith((b"\x1aE\xdf\xa3", b"FLV", b"OggS", b"\x00\x00\x01\xba", b"\x06\x0e\x2b\x34")):
        return True
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] in {b"AVI ", b"AVIX"}:
        return True
    return len(head) > 376 and head[0] == head[188] == head[376] == 0x47


def _rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None


def probe(
    path: Path,
    timeout: float = 15,
    canceled: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], list[ProvenanceEvidence], str | None]:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,start_time,format_name:format_tags=creation_time,date,location,com.apple.quicktime.creationdate:"
        "stream=index,codec_type,codec_name,time_base,start_pts,start_time,duration_ts,duration,width,height,"
        "avg_frame_rate,r_frame_rate,sample_aspect_ratio,pix_fmt,color_space,color_transfer,color_primaries,"
        "field_order,sample_rate,channels,channel_layout:stream_tags=creation_time,handler_name,rotate:stream_side_data=rotation",
        "-of", "json", str(path),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        return {}, [], f"ffprobe unavailable ({error.errno or 'unknown'})"
    deadline = time.monotonic() + timeout
    while True:
        if canceled and canceled():
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=1)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return {}, [], "Probe canceled"
        try:
            stdout, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() < deadline:
                continue
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return {}, [], "ffprobe timed out"
    if len(stdout) > 2_000_000 or len(stderr) > 200_000:
        return {}, [], "ffprobe output exceeded safe bounds"
    if process.returncode != 0:
        return {}, [], f"ffprobe could not decode this asset (exit {process.returncode})"
    try:
        payload = json.loads(stdout)
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
        evidence.append(
            ProvenanceEvidence(
                "container", "captured_at", captured, 0.85, "format.tags", raw_value=captured,
                normalized_value=captured, uncertainty=None if str(captured).endswith(("Z", "+00:00")) else "timezone may be absent",
            )
        )
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
    root_id: str | None = None,
    mode: str = "FULL",
    existing_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    existing_batch_lookup: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
    canceled: Callable[[], bool] | None = None,
    max_files: int | None = None,
    probe_workers: int = 4,
) -> Iterable[MediaRecord]:
    resolved = root.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Library root must be a directory")
    worker_count = max(1, min(int(probe_workers), 8))
    pending: set[Future[MediaRecord]] = set()
    local_stop = threading.Event()
    should_stop = lambda: local_stop.is_set() or bool(canceled and canceled())
    discovered = 0
    paths = iter(iter_videos(resolved))
    exhausted = False
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="media-probe")
    try:
        while pending or not exhausted:
            capacity = worker_count * 2 - len(pending)
            candidates: list[tuple[Path, str, dict[str, Any]]] = []
            while not exhausted and len(candidates) < capacity:
                if should_stop():
                    exhausted = True
                    break
                if max_files is not None and discovered >= max_files:
                    exhausted = True
                    break
                try:
                    path = next(paths)
                except StopIteration:
                    exhausted = True
                    break
                discovered += 1
                relative = path.relative_to(resolved)
                relative_text = relative.as_posix()
                try:
                    cheap = cheap_fingerprint(path)
                except OSError:
                    cheap = {}
                candidates.append((path, relative_text, cheap))
            existing_by_path = (
                existing_batch_lookup([item[1] for item in candidates])
                if existing_batch_lookup and candidates
                else {}
            )
            for path, relative_text, cheap in candidates:
                existing = existing_by_path.get(relative_text)
                if existing is None and existing_lookup:
                    existing = existing_lookup(relative_text)
                if mode == "INCREMENTAL" and existing and _fingerprint_matches(existing.get("fingerprint", {}), cheap):
                    cached = media_record_from_dict(existing, library_id)
                    cached.root_id = root_id
                    yield cached
                    continue
                if root_id is None:
                    pending.add(executor.submit(_scan_path, resolved, path, library_id, should_stop))
                else:
                    pending.add(
                        executor.submit(_scan_path, resolved, path, library_id, should_stop, root_id)
                    )
            if not pending:
                continue
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                if should_stop():
                    for queued in pending:
                        queued.cancel()
                    return
                yield future.result()
    finally:
        local_stop.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)


def _scan_path(
    root: Path,
    path: Path,
    library_id: str,
    canceled: Callable[[], bool] | None,
    root_id: str | None = None,
) -> MediaRecord:
    relative = path.relative_to(root)
    relative_text = relative.as_posix()
    try:
        resolved_path = path.resolve(strict=True)
        if not resolved_path.is_relative_to(root) or not resolved_path.is_file():
            stat = path.lstat()
            return MediaRecord(
                id=stable_media_id(library_id, relative_text, root_id),
                library_id=library_id,
                root_id=root_id,
                relative_path=relative_text,
                size=stat.st_size,
                modified_ns=stat.st_mtime_ns,
                warning="Source path resolves outside the authorized library root",
                source_candidate_id=f"candidate_{digest_json({'parent': relative.parent.as_posix()})[:24]}",
            )
        fingerprint = quick_fingerprint(resolved_path)
        inferred = infer_from_path(resolved_path, relative)
        sidecar = read_sidecar(resolved_path, relative)
        probed_values, probed_evidence, warning = probe(resolved_path, canceled=canceled)
        values, evidence = merge_evidence(inferred, (probed_values, probed_evidence), sidecar)
        camera_evidence = sorted(
            {
                (item.kind, str(item.normalized_value if item.normalized_value is not None else item.value).strip().casefold())
                for item in evidence
                if item.field == "camera" and (item.normalized_value is not None or item.value is not None)
            }
        )
        candidate_material = (
            {"cameraEvidence": camera_evidence}
            if camera_evidence
            else {"parent": relative.parent.as_posix().casefold()}
        )
        stat = resolved_path.stat()
        return MediaRecord(
            id=stable_media_id(library_id, relative_text, root_id),
            library_id=library_id,
            root_id=root_id,
            relative_path=relative_text,
            size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            warning=warning,
            evidence=evidence,
            fingerprint=fingerprint,
            source_candidate_id=f"candidate_{digest_json(candidate_material)[:24]}",
            **values,
        )
    except OSError as error:
        try:
            stat = path.lstat()
            size, modified_ns = stat.st_size, stat.st_mtime_ns
        except OSError:
            size, modified_ns = 0, 0
        return MediaRecord(
            id=stable_media_id(library_id, relative_text, root_id),
            library_id=library_id,
            root_id=root_id,
            relative_path=relative_text,
            size=size,
            modified_ns=modified_ns,
            warning=f"Source could not be inspected ({error.errno or 'unknown'})",
            source_candidate_id=f"candidate_{digest_json({'parent': relative.parent.as_posix()})[:24]}",
        )


def _fingerprint_matches(existing: dict[str, Any], current: dict[str, Any]) -> bool:
    keys = {"device", "inode", "size", "modifiedNs", "sidecar", "probeVersion"}
    return all(existing.get(key) == current.get(key) for key in keys)


def media_record_from_dict(value: dict[str, Any], library_id: str | None = None) -> MediaRecord:
    data = dict(value)
    data["library_id"] = library_id or data.pop("libraryId", data.get("library_id"))
    data["duration_us"] = data.pop("durationUs", data.get("duration_us"))
    data["source_candidate_id"] = data.pop("sourceCandidateId", data.get("source_candidate_id"))
    data["root_id"] = data.pop("rootId", data.get("root_id"))
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
    allowed = {item.name for item in fields(ProvenanceEvidence)}
    normalized = {aliases.get(key, key): item for key, item in value.items()}
    custom = dict(normalized.get("custom") or {})
    for key in tuple(normalized):
        if key not in allowed:
            custom[key] = normalized.pop(key)
    normalized["custom"] = custom
    return normalized


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
