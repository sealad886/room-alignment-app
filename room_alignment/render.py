from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .store import Store


class PreflightError(ValueError):
    pass


def _safe_source(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve(strict=True)
    if not candidate.is_relative_to(root.resolve()):
        raise PreflightError("Source path escapes library root")
    if not candidate.is_file():
        raise PreflightError(f"Source is unavailable: {relative}")
    return candidate


def preflight(store: Store, project: dict[str, Any]) -> dict[str, Any]:
    root = store.library_root(project["libraryId"])
    video = sorted(project.get("videoSegments", []), key=lambda item: item["start"])
    audio = sorted(project.get("audioSegments", []), key=lambda item: item["start"])
    issues: list[dict[str, str]] = []
    if not video:
        issues.append({"kind": "missing-video", "message": "Program Video has no segments"})
    cursor = 0.0
    for segment in video:
        start, end = float(segment["start"]), float(segment["end"])
        if start > cursor + 0.001:
            issues.append({"kind": "gap", "message": f"Video gap at {cursor:.3f}s–{start:.3f}s"})
        if start < cursor - 0.001:
            issues.append({"kind": "overlap", "message": f"Video overlap begins at {start:.3f}s"})
        cursor = max(cursor, end)
        try:
            media = store.media_record(segment["mediaId"])
            _safe_source(root, media["relative_path"])
            required_duration = float(segment.get("sourceIn", 0)) + end - start
            if media.get("duration") is not None and required_duration > float(media["duration"]) + 0.05:
                issues.append({"kind": "missing-coverage", "message": f"{segment.get('id', 'segment')} exceeds source duration by {required_duration - float(media['duration']):.3f}s"})
        except (KeyError, OSError, PreflightError) as error:
            issues.append({"kind": "missing-media", "message": str(error)})
        required = ("id", "mediaId", "start", "end", "sourceIn")
        if any(key not in segment for key in required):
            issues.append({"kind": "provenance", "message": f"Video segment lacks provenance fields: {segment.get('id', 'unknown')}"})
    for segment in audio:
        if segment.get("mediaId") is None:
            continue
        try:
            media = store.media_record(segment["mediaId"])
            _safe_source(root, media["relative_path"])
            if not media.get("audio_codec"):
                issues.append({"kind": "missing-audio", "message": f"{segment.get('id', 'audio segment')} selects a source without an audio stream"})
        except (KeyError, OSError, PreflightError) as error:
            issues.append({"kind": "missing-audio", "message": str(error)})
    return {
        "valid": not issues,
        "issues": issues,
        "duration": cursor,
        "videoCuts": max(0, len(video) - 1),
        "independentAudioEdits": sum(1 for item in audio if not item.get("linked", True)),
        "renderMode": "reencode",
        "sourceMediaUnchanged": True,
    }


def build_manifest(store: Store, project: dict[str, Any]) -> dict[str, Any]:
    root = store.library_root(project["libraryId"])

    def enrich(segment: dict[str, Any], media_kind: str) -> dict[str, Any]:
        item = dict(segment)
        media_id = item.get("mediaId")
        if media_id is None:
            item["provenance"] = {"mediaKind": media_kind, "source": "silence", "transforms": ["generated silence"]}
            return item
        media = store.media_record(media_id)
        item["provenance"] = {
            "mediaKind": media_kind,
            "mediaId": media_id,
            "libraryRelativePath": media["relative_path"],
            "sourceClipId": media["id"],
            "camera": media.get("camera"),
            "capturedAt": media.get("captured_at"),
            "sourceIn": item.get("sourceIn", 0),
            "sourceOut": item.get("sourceIn", 0) + float(item["end"]) - float(item["start"]),
            "editorialIn": item["start"],
            "editorialOut": item["end"],
            "syncOffsetMs": item.get("syncOffsetMs", 0),
            "evidence": media.get("evidence", []),
            "custom": media.get("custom", {}),
            "transforms": item.get("transforms", []),
        }
        return item

    check = preflight(store, project)
    return {
        "schema": "room-alignment-edit-decision/v1",
        "project": {"id": project["id"], "name": project["name"]},
        "library": {"id": project["libraryId"], "rootHint": root.name},
        "timebase": project.get("timebase", {"kind": "seconds", "origin": project.get("wallClockOrigin")}),
        "cutAnchoring": project.get("cutAnchoring", "wall-clock"),
        "alignment": project.get("alignment", {}),
        "videoSegments": [enrich(item, "video") for item in project.get("videoSegments", [])],
        "audioSegments": [enrich(item, "audio") for item in project.get("audioSegments", [])],
        "preflight": check,
        "fidelity": {
            "sourceMediaUnchanged": True,
            "plannedVideo": "decode and re-encode at source-compatible dimensions/frame rate",
            "plannedAudio": "decode and encode; offsets, channel mapping, and sample-rate conversion disclosed per segment",
        },
    }


def build_ffmpeg_command(store: Store, project: dict[str, Any], output: Path, lossless: bool = False) -> list[str]:
    check = preflight(store, project)
    if not check["valid"]:
        raise PreflightError("Render blocked: " + "; ".join(item["message"] for item in check["issues"]))
    root = store.library_root(project["libraryId"])
    video = sorted(project["videoSegments"], key=lambda item: item["start"])
    audio = sorted(project.get("audioSegments", []), key=lambda item: item["start"])
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    filters: list[str] = []
    first_media = store.media_record(video[0]["mediaId"])
    target_width = int(first_media.get("width") or 1920)
    target_height = int(first_media.get("height") or 1080)
    target_fps = float(first_media.get("frame_rate") or 30)
    for index, segment in enumerate(video):
        media = store.media_record(segment["mediaId"])
        source = _safe_source(root, media["relative_path"])
        duration = float(segment["end"]) - float(segment["start"])
        command += ["-ss", str(float(segment.get("sourceIn", 0))), "-t", str(duration), "-i", str(source)]
        filters.append(
            f"[{index}:v:0]setpts=PTS-STARTPTS,"
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={target_fps:.6f},setsar=1[v{index}]"
        )
    video_refs = "".join(f"[v{i}]" for i in range(len(video)))
    filters.append(f"{video_refs}concat=n={len(video)}:v=1:a=0[vout]")
    audio_base = len(video)
    if audio:
        for index, segment in enumerate(audio):
            duration = float(segment["end"]) - float(segment["start"])
            if segment.get("mediaId") is None:
                command += ["-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=48000:cl=stereo"]
            else:
                media = store.media_record(segment["mediaId"])
                source = _safe_source(root, media["relative_path"])
                source_in = float(segment.get("sourceIn", 0)) + float(segment.get("offsetMs", 0)) / 1000
                command += ["-ss", str(max(0, source_in)), "-t", str(duration), "-i", str(source)]
            filters.append(f"[{audio_base + index}:a:0]asetpts=PTS-STARTPTS,aresample=48000[a{index}]")
        audio_refs = "".join(f"[a{i}]" for i in range(len(audio)))
        filters.append(f"{audio_refs}concat=n={len(audio)}:v=0:a=1[aout]")
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if audio:
        command += ["-map", "[aout]"]
    if lossless:
        command += ["-c:v", "ffv1", "-level", "3"]
        if audio:
            command += ["-c:a", "pcm_s24le"]
    else:
        command += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac", "-b:a", "192k"]
    command += ["-movflags", "+faststart"] if output.suffix.lower() == ".mp4" else []
    command.append(str(output))
    return command


@dataclass
class RunningJob:
    process: subprocess.Popen[str]
    cancel_requested: bool = False


class RenderManager:
    def __init__(self, store: Store):
        self.store = store
        self.jobs: dict[str, RunningJob] = {}
        self.lock = threading.RLock()

    def start(self, project: dict[str, Any], output: Path, lossless: bool = False) -> str:
        output = output.expanduser().resolve()
        if lossless and output.suffix.lower() != ".mkv":
            output = output.with_suffix(".mkv")
        if output.exists():
            raise PreflightError(f"Output already exists; choose a new path: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "projectId": project["id"], "status": "queued", "outputPath": str(output), "progress": 0, "message": "Preflight complete"}
        self.store.upsert_job(job)
        thread = threading.Thread(target=self._run, args=(job_id, project, output, lossless), daemon=True)
        thread.start()
        return job_id

    def _run(self, job_id: str, project: dict[str, Any], output: Path, lossless: bool) -> None:
        partial = output.with_name(output.stem + ".partial" + output.suffix)
        try:
            command = build_ffmpeg_command(self.store, project, partial, lossless)
            self.store.upsert_job({"id": job_id, "projectId": project["id"], "status": "running", "outputPath": str(output), "progress": 0.1, "message": "Rendering"})
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
            with self.lock:
                self.jobs[job_id] = RunningJob(process)
            _, stderr = process.communicate()
            with self.lock:
                canceled = self.jobs.get(job_id, RunningJob(process)).cancel_requested
                self.jobs.pop(job_id, None)
            if canceled:
                partial.unlink(missing_ok=True)
                self.store.upsert_job({"id": job_id, "projectId": project["id"], "status": "canceled", "outputPath": str(output), "progress": 0, "message": "Canceled; partial output removed"})
            elif process.returncode != 0:
                partial.unlink(missing_ok=True)
                detail = stderr.strip().splitlines()[-1] if stderr.strip() else "ffmpeg failed"
                raise RuntimeError(detail[:500])
            else:
                os.replace(partial, output)
                manifest_path = output.with_suffix(output.suffix + ".manifest.json")
                manifest_path.write_text(json.dumps(build_manifest(self.store, project), indent=2), encoding="utf-8")
                self.store.upsert_job({"id": job_id, "projectId": project["id"], "status": "complete", "outputPath": str(output), "progress": 1, "message": f"Rendered with manifest: {manifest_path.name}"})
        except Exception as error:
            partial.unlink(missing_ok=True)
            self.store.upsert_job({"id": job_id, "projectId": project["id"], "status": "failed", "outputPath": str(output), "progress": 0, "message": str(error)})

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            running = self.jobs.get(job_id)
            if not running:
                return False
            running.cancel_requested = True
            try:
                os.killpg(running.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return False
            return True
