from __future__ import annotations

import errno
import json
import math
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import __version__
from .domain import (
    DomainError,
    alignment_digest,
    compile_program,
    digest_json,
    now_iso,
    opaque_id,
)
from .scanner import full_digest
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
    term_sent_at: float | None = None
    kill_sent: bool = False


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


def build_render_plan(
    store: Store,
    project_id: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    project = store.project(project_id)
    assets = store.media_records(item["assetId"] for item in project["clips"])
    compiled = compile_program(project, assets)
    output_grant_id = str(settings.get("outputGrantId", ""))
    filename = str(settings.get("filename", ""))
    profile = str(settings.get("profile", project.get("renderSettings", {}).get("profile", "COMPATIBLE")))
    if profile not in {"COMPATIBLE", "ARCHIVAL_LOSSLESS"}:
        raise DomainError("VALIDATION_FAILED", "Unknown output profile")
    output = store.output_path(output_grant_id, filename)
    expected_suffix = ".mp4" if profile == "COMPATIBLE" else ".mkv"
    if output.suffix.lower() != expected_suffix:
        raise DomainError("VALIDATION_FAILED", f"{profile} output filename must end with {expected_suffix}")
    issues = list(compiled["issues"])
    current_alignment_digest = alignment_digest(project)
    current_selection_digest = str((project.get("selectionSnapshot") or {}).get("digest", ""))
    current_sections_digest = digest_json(project.get("timelineSections", []))
    program_draft = project.get("programDraft")
    if program_draft and (
        str(program_draft.get("selectionDigest", "")) != current_selection_digest
        or str(program_draft.get("alignmentDigest", "")) != current_alignment_digest
        or str(program_draft.get("timelineSectionsDigest", "")) != current_sections_digest
    ):
        issues.append(
            {
                "id": "issue_program_dependencies_stale",
                "code": "PLAN_STALE",
                "severity": "BLOCKING",
                "message": "Program decisions are stale relative to selection, alignment, or composition",
            }
        )
    if output.exists() or output.with_name(output.name + ".manifest.json").exists():
        issues.append(
            {
                "id": "issue_destination_exists",
                "code": "DESTINATION_EXISTS",
                "severity": "BLOCKING",
                "message": "Output video or manifest already exists",
            }
        )
    selected_ids = sorted(
        {
            item["assetId"]
            for item in compiled["videoSlices"] + compiled["audioSlices"]
            if item.get("assetId")
        }
    )
    root = store.library_root(project["libraryId"])
    sources: list[dict[str, Any]] = []
    for media_id in selected_ids:
        media = assets[media_id]
        source = _safe_source(root, media["relative_path"])
        digest = full_digest(source)
        sources.append(
            {
                "assetId": media_id,
                "libraryRelativePath": media["relative_path"],
                "size": source.stat().st_size,
                "modifiedNs": source.stat().st_mtime_ns,
                "sha256": digest,
                "fingerprint": media.get("fingerprint", {}),
            }
        )
    for source_slice in compiled["videoSlices"] + compiled["audioSlices"]:
        if source_slice.get("synthetic"):
            continue
        if not source_slice.get("streamId"):
            issues.append(
                {
                    "id": f"issue_stream_{source_slice['id']}",
                    "code": "PROVENANCE_UNRESOLVED",
                    "severity": "BLOCKING",
                    "message": "Selected source stream identity is unresolved",
                }
            )
    first_recorded_video = next(
        (item for item in compiled["videoSlices"] if not item.get("synthetic")),
        None,
    )
    first_video = assets.get(first_recorded_video["assetId"], {}) if first_recorded_video else {}
    width = settings.get("width", first_video.get("width") or 1920)
    height = settings.get("height", first_video.get("height") or 1080)
    frame_rate = settings.get("frameRate", first_video.get("frame_rate") or 30)
    if isinstance(width, bool) or not isinstance(width, int) or width < 16:
        raise DomainError("VALIDATION_FAILED", "Render width must be an integer of at least 16 pixels")
    if isinstance(height, bool) or not isinstance(height, int) or height < 16:
        raise DomainError("VALIDATION_FAILED", "Render height must be an integer of at least 16 pixels")
    if (
        isinstance(frame_rate, bool)
        or not isinstance(frame_rate, (int, float))
        or not math.isfinite(float(frame_rate))
        or float(frame_rate) < 1
        or float(frame_rate) > 240
    ):
        raise DomainError("VALIDATION_FAILED", "Render frameRate must be finite and between 1 and 240")
    normalization = {
        "width": width,
        "height": height,
        "frameRate": float(frame_rate),
        "frameRatePolicy": "CONSTANT",
        "aspectPolicy": "FIT_AND_PAD",
        "rotationPolicy": "APPLY_DISPLAY_ROTATION",
        "sampleAspectRatio": "1:1",
        "colorPolicy": "PRESERVE_WHEN_COMPATIBLE",
        "hdrPolicy": "BLOCK_UNDECLARED_CONVERSION",
        "pixelFormat": "yuv420p" if profile == "COMPATIBLE" else "source-compatible",
        "audioSampleRate": 48_000,
        "audioChannelLayout": "stereo",
    }
    warning_codes: list[str] = []
    for source_slice in compiled["videoSlices"]:
        if source_slice.get("synthetic"):
            continue
        media = assets[source_slice["assetId"]]
        for stream in media.get("streams", []):
            if stream.get("codecType") != "video":
                continue
            if stream.get("colorTransfer") in {"smpte2084", "arib-std-b67"}:
                issues.append(
                    {
                        "id": f"issue_hdr_{source_slice['assetId']}",
                        "code": "UNSUPPORTED_MEDIA",
                        "severity": "BLOCKING",
                        "message": "HDR source requires an explicit supported color-conversion policy",
                    }
                )
            if stream.get("rotation") not in {None, 0, "0"}:
                warning_codes.append("ROTATION_APPLIED")
    estimate = _estimate_output_bytes(compiled["durationUs"], profile)
    free = shutil.disk_usage(output.parent).free
    if free < int(estimate * 1.25) + 64 * 1024 * 1024:
        issues.append(
            {
                "id": "issue_insufficient_space",
                "code": "INSUFFICIENT_SPACE",
                "severity": "BLOCKING",
                "message": "Output directory lacks estimated temporary and final space",
            }
        )
    plan_body = {
        "schema": "room-alignment-render-plan/v1",
        "projectId": project["id"],
        "projectRevision": project["revision"],
        "provenanceRevision": project.get("provenanceRevision", 0),
        "selectionSnapshot": project.get("selectionSnapshot", {}),
        "selectionDigest": current_selection_digest,
        "alignmentDigest": current_alignment_digest,
        "clipAlignments": [
            {
                "clipId": item["id"],
                "assetId": item["assetId"],
                "logicalSourceId": item["logicalSourceId"],
                "alignment": item.get("alignment") or item.get("sync"),
                "alignmentState": item.get(
                    "alignmentState", "ACCEPTED" if item.get("sync") else "UNRESOLVED"
                ),
                "alignmentConfidence": item.get("alignmentConfidence"),
                "alignmentEvidence": item.get("alignmentEvidence", []),
            }
            for item in project.get("clips", [])
        ],
        "timelineSections": project.get("timelineSections", []),
        "timelineSectionsDigest": current_sections_digest,
        "programDraft": program_draft,
        "syntheticSlates": project.get("syntheticSlates", []),
        "compiledProgram": compiled,
        "sources": sources,
        "sourceSetDigest": digest_json(sources),
        "provenanceResolutions": store.provenance_snapshot(selected_ids),
        "profile": profile,
        "container": "mp4" if profile == "COMPATIBLE" else "matroska",
        "videoCodec": "h264" if profile == "COMPATIBLE" else "ffv1",
        "audioCodec": "aac" if profile == "COMPATIBLE" else "pcm_s24le",
        "normalization": normalization,
        "output": {"grantId": output_grant_id, "filename": filename},
        "estimatedBytes": estimate,
        "warningCodes": sorted(set(warning_codes)),
        "toolVersions": {
            "application": f"room-alignment/{__version__}",
            "ffmpeg": _tool_version("ffmpeg"),
            "ffprobe": _tool_version("ffprobe"),
        },
        "issues": issues,
        "status": "BLOCKED" if any(item.get("severity") == "BLOCKING" for item in issues) else "READY",
        "createdAt": now_iso(),
    }
    plan = {"id": opaque_id("plan"), **plan_body}
    plan["planDigest"] = digest_json({key: value for key, value in plan_body.items() if key != "createdAt"})
    return store.save_render_plan(plan)


def build_v1_manifest(plan: dict[str, Any], artifact: dict[str, Any] | None = None) -> dict[str, Any]:
    program = plan["compiledProgram"]
    manifest = {
        "schema": "room-alignment-provenance-manifest/v1",
        "manifestCanonicalization": "room-alignment-canonical-json/v1",
        "artifact": {
            "id": artifact.get("id") if artifact else None,
            "videoSha256": artifact.get("videoDigest") if artifact else None,
            "manifestSha256": None,
            "manifestFileDigestRecordedInArtifactState": True,
        },
        "project": {
            "id": plan["projectId"],
            "revision": plan["projectRevision"],
            "provenanceRevision": plan["provenanceRevision"],
            "selectionDigest": plan.get("selectionDigest", ""),
            "alignmentDigest": plan.get("alignmentDigest", ""),
            "timelineSectionsDigest": plan.get("timelineSectionsDigest", ""),
        },
        "renderPlan": {"id": plan["id"], "digest": plan["planDigest"]},
        "sourceSetDigest": plan["sourceSetDigest"],
        "provenanceResolutions": plan.get("provenanceResolutions", []),
        "selectionSnapshot": plan.get("selectionSnapshot", {}),
        "alignment": {
            "digest": plan.get("alignmentDigest", ""),
            "clipTransforms": plan.get("clipAlignments", []),
        },
        "composition": {
            "timelineSections": plan.get("timelineSections", []),
            "timelineSectionsDigest": plan.get("timelineSectionsDigest", ""),
            "programDraft": plan.get("programDraft"),
            "syntheticSlates": plan.get("syntheticSlates", []),
        },
        "sources": [
            {
                "assetId": item["assetId"],
                "libraryRelativePath": item["libraryRelativePath"],
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in plan["sources"]
        ],
        "outputTimebase": {"unit": "microseconds", "intervals": "half-open"},
        "videoSlices": program["videoSlices"],
        "audioSlices": program["audioSlices"],
        "transforms": {
            "profile": plan["profile"],
            "container": plan["container"],
            "videoCodec": plan["videoCodec"],
            "audioCodec": plan["audioCodec"],
            "normalization": plan["normalization"],
            "sourceMediaUnchanged": True,
            "streamCopy": False,
            "decodedAndReencoded": True,
        },
        "warnings": plan["warningCodes"],
        "toolVersions": plan.get("toolVersions", {}),
        "fidelity": {
            "claim": "lossless-encode-after-processing" if plan["profile"] == "ARCHIVAL_LOSSLESS" else "compatible-reencode",
            "sourceFilesModified": False,
            "generatedSilenceDisclosed": any(item.get("synthetic") for item in program["audioSlices"]),
            "generatedSlateDisclosed": any(item.get("synthetic") for item in program["videoSlices"]),
        },
    }
    manifest["manifestCanonicalContentSha256"] = digest_json(manifest)
    return manifest


def build_v1_ffmpeg_command(store: Store, plan: dict[str, Any], output: Path) -> list[str]:
    if plan["status"] != "READY":
        raise DomainError("COVERAGE_INVALID", "Render plan has blocking issues")
    project = store.project(plan["projectId"])
    root = store.library_root(project["libraryId"])
    planned_paths = {item["assetId"]: item["libraryRelativePath"] for item in plan["sources"]}
    video = plan["compiledProgram"]["videoSlices"]
    audio = plan["compiledProgram"]["audioSlices"]
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    filters: list[str] = []
    norm = plan["normalization"]
    for item in video:
        if item.get("synthetic"):
            output_duration = (item["endUs"] - item["startUs"]) / 1_000_000
            command += [
                "-f",
                "lavfi",
                "-t",
                f"{output_duration:.6f}",
                "-i",
                (
                    f"color=c=0x141922:s={norm['width']}x{norm['height']}:"
                    f"r={norm['frameRate']:.6f}"
                ),
            ]
        else:
            source = _planned_source(root, planned_paths, item["assetId"])
            source_duration = (item["sourceEndUs"] - item["sourceStartUs"]) / 1_000_000
            command += ["-ss", _seconds(item["sourceStartUs"]), "-t", f"{source_duration:.6f}", "-i", str(source)]
    audio_base = len(video)
    for item in audio:
        output_duration = (item["endUs"] - item["startUs"]) / 1_000_000
        if item.get("synthetic"):
            command += ["-f", "lavfi", "-t", f"{output_duration:.6f}", "-i", "anullsrc=r=48000:cl=stereo"]
        else:
            source = _planned_source(root, planned_paths, item["assetId"])
            source_start = int(item["sourceStartUs"])
            source_duration = (item["sourceEndUs"] - item["sourceStartUs"]) / 1_000_000
            command += ["-ss", _seconds(source_start), "-t", f"{source_duration:.6f}", "-i", str(source)]
    for index, item in enumerate(video):
        output_duration = (item["endUs"] - item["startUs"]) / 1_000_000
        if item.get("synthetic"):
            slate_card = _slate_drawbox_chain(
                str(item.get("slateText") or "No recorded footage"),
                int(norm["width"]),
                int(norm["height"]),
            )
            filters.append(
                f"[{index}:v:0]setpts=PTS-STARTPTS,"
                f"{slate_card},"
                f"fps={norm['frameRate']:.6f},setsar=1[v{index}]"
            )
        else:
            source_duration = (item["sourceEndUs"] - item["sourceStartUs"]) / 1_000_000
            speed = output_duration / source_duration if source_duration else 1
            filters.append(
                f"[{index}:v:0]setpts=(PTS-STARTPTS)*{speed:.12f},"
                f"scale={norm['width']}:{norm['height']}:force_original_aspect_ratio=decrease,"
                f"pad={norm['width']}:{norm['height']}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={norm['frameRate']:.6f},setsar=1[v{index}]"
            )
    filters.append(f"{''.join(f'[v{i}]' for i in range(len(video)))}concat=n={len(video)}:v=1:a=0[vout]")
    for index, item in enumerate(audio):
        input_index = audio_base + index
        output_duration = (item["endUs"] - item["startUs"]) / 1_000_000
        source_duration = (
            (item.get("sourceEndUs", item["endUs"]) - item.get("sourceStartUs", item["startUs"])) / 1_000_000
        )
        tempo = source_duration / output_duration if output_duration else 1
        chain = f"[{input_index}:a:0]asetpts=PTS-STARTPTS,aresample=48000"
        if not item.get("synthetic") and abs(tempo - 1) > 0.000001:
            chain += f",atempo={tempo:.12f}"
        chain += f",apad,atrim=duration={output_duration:.6f}[a{index}]"
        filters.append(chain)
    if audio:
        filters.append(f"{''.join(f'[a{i}]' for i in range(len(audio)))}concat=n={len(audio)}:v=0:a=1[aout]")
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if audio:
        command += ["-map", "[aout]"]
    if plan["profile"] == "ARCHIVAL_LOSSLESS":
        command += ["-c:v", "ffv1", "-level", "3"]
        if audio:
            command += ["-c:a", "pcm_s24le"]
    else:
        command += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac", "-b:a", "192k"]
        command += ["-movflags", "+faststart"]
    command.append(str(output))
    return command


_PIXEL_FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "?": ("01110", "10001", "00010", "00100", "00100", "00000", "00100"),
}


def _slate_drawbox_chain(value: str, width: int, height: int) -> str:
    """Render a portable two-line bitmap card with core FFmpeg filters only."""

    words = [word for word in value.upper().split() if word]
    if words == ["NO", "RECORDED", "FOOTAGE"]:
        lines = ["NO RECORDED", "FOOTAGE"]
    else:
        midpoint = max(1, (len(words) + 1) // 2)
        lines = [" ".join(words[:midpoint]), " ".join(words[midpoint:])]
        lines = [line[:24] for line in lines if line] or ["NO FOOTAGE"]
    columns = max((len(line) * 6 - 1 for line in lines), default=1)
    scale = max(1, min(width // max(columns + 8, 1), height // 22))
    line_height = 7 * scale
    line_gap = 3 * scale
    total_height = len(lines) * line_height + (len(lines) - 1) * line_gap
    padding = max(4, 3 * scale)
    widest = columns * scale
    box_width = min(width, widest + padding * 2)
    box_height = min(height, total_height + padding * 2)
    box_x = max(0, (width - box_width) // 2)
    box_y = max(0, (height - box_height) // 2)
    filters = [
        f"drawbox=x={box_x}:y={box_y}:w={box_width}:h={box_height}:color=black@0.62:t=fill"
    ]
    text_y = max(0, (height - total_height) // 2)
    for line_index, line in enumerate(lines):
        bits = "0".join("00000" if character == " " else _PIXEL_FONT.get(character, _PIXEL_FONT["?"])[0] for character in line)
        line_columns = len(bits)
        text_x = max(0, (width - line_columns * scale) // 2)
        for row_index in range(7):
            row_bits = "0".join(
                "00000"
                if character == " "
                else _PIXEL_FONT.get(character, _PIXEL_FONT["?"])[row_index]
                for character in line
            )
            run_start: int | None = None
            for column, bit in enumerate(row_bits + "0"):
                if bit == "1" and run_start is None:
                    run_start = column
                elif bit == "0" and run_start is not None:
                    filters.append(
                        "drawbox="
                        f"x={text_x + run_start * scale}:"
                        f"y={text_y + line_index * (line_height + line_gap) + row_index * scale}:"
                        f"w={(column - run_start) * scale}:h={scale}:color=white:t=fill"
                    )
                    run_start = None
    return ",".join(filters)


def _planned_source(root: Path, planned_paths: dict[str, str], asset_id: str) -> Path:
    relative = planned_paths.get(asset_id)
    if relative is None:
        raise DomainError("PLAN_STALE", "Selected source is absent from the immutable render plan")
    return _safe_source(root, relative)


class CanonicalRenderManager:
    def __init__(self, store: Store):
        self.store = store
        self.jobs: dict[str, RunningJob] = {}
        self.reserved: set[str] = set()
        self.lock = threading.RLock()
        self.reconcile_artifacts()

    def start(self, plan_id: str) -> dict[str, Any]:
        plan = self.store.render_plan(plan_id)
        if plan["status"] != "READY":
            raise DomainError("COVERAGE_INVALID", "Render plan has blocking issues")
        review = self.store.review_for_plan(plan_id)
        if not review or review["planDigest"] != plan["planDigest"]:
            raise DomainError("REVIEW_STALE", "Render plan requires a current review attestation")
        project = self.store.project(plan["projectId"])
        self.store.library_root(project["libraryId"])
        if project["revision"] != plan["projectRevision"]:
            raise DomainError("PLAN_STALE", "Project changed after review")
        if int(project.get("provenanceRevision", 0)) != int(plan["provenanceRevision"]):
            raise DomainError("PLAN_STALE", "Provenance resolution changed after review")
        output = plan["output"]
        final = self.store.output_path(output["grantId"], output["filename"])
        manifest = final.with_name(final.name + ".manifest.json")
        if final.exists() or manifest.exists():
            raise DomainError("DESTINATION_EXISTS", "Output video or manifest already exists")
        with self.lock:
            if self.reserved or self.jobs:
                raise DomainError("JOB_STATE_CONFLICT", "Only one render may run at a time")
            artifact = self.store.create_artifact(plan_id, output["grantId"], output["filename"])
            job = self.store.create_job("RENDER", project_id=plan["projectId"], message="Render queued")
            artifact = self.store.update_artifact(artifact["id"], job_id=job["id"], status="QUEUED")
            self.reserved.add(job["id"])
        thread = threading.Thread(target=self._run_owned, args=(job["id"], artifact["id"], plan), daemon=True)
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.reserved.discard(job["id"])
            self.store.update_artifact(artifact["id"], status="FAILED", details_json={"error": "THREAD_START_FAILED"})
            self.store.transition_job(job["id"], "FAILED", 0, "Render worker could not start")
            raise
        return {"job": job, "artifact": artifact}

    def _run_owned(self, job_id: str, artifact_id: str, plan: dict[str, Any]) -> None:
        try:
            self._run(job_id, artifact_id, plan)
        finally:
            with self.lock:
                self.reserved.discard(job_id)

    def _run(self, job_id: str, artifact_id: str, plan: dict[str, Any]) -> None:
        artifact = self.store.artifact(artifact_id)
        if self.store.job(job_id)["status"] == "CANCEL_REQUESTED":
            self._finish_cancellation(job_id, artifact_id, "before output access")
            return
        try:
            final = self.store.output_path(artifact["outputGrantId"], artifact["filename"])
        except DomainError as error:
            self.store.update_artifact(artifact_id, status="FAILED", details_json={"errorCode": error.code})
            self.store.transition_job(job_id, "FAILED", 0, str(error), error_code=error.code)
            return
        manifest_final = final.with_name(artifact["manifestFilename"])
        token = artifact_id.rsplit("_", 1)[-1]
        partial = final.with_name(f".{final.stem}.partial.{token}{final.suffix}")
        manifest_partial = final.with_name(f".{manifest_final.name}.partial.{token}")
        video_promoted = False
        manifest_promoted = False
        try:
            if self.store.job(job_id)["status"] == "CANCEL_REQUESTED":
                self._finish_cancellation(job_id, artifact_id, "before render process launch")
                return
            self.store.transition_job(job_id, "RUNNING", 0.05, "Validating immutable sources")
            self.store.update_artifact(artifact_id, status="RENDERING")
            self._validate_sources(plan)
            command = build_v1_ffmpeg_command(self.store, plan, partial)
            with tempfile.SpooledTemporaryFile(mode="w+t", max_size=1_000_000) as diagnostics:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=diagnostics,
                    text=True,
                    start_new_session=True,
                )
                running = RunningJob(process)
                with self.lock:
                    self.jobs[job_id] = running
                    if self.store.job(job_id)["status"] == "CANCEL_REQUESTED":
                        self._request_process_stop(running)
                space_failure = False
                next_progress_update = time.monotonic()
                continuation_floor = 64 * 1024 * 1024 + int(plan["estimatedBytes"] * 0.05)
                while process.poll() is None:
                    current_job = self.store.job(job_id)
                    if current_job["status"] == "CANCEL_REQUESTED":
                        self._request_process_stop(running)
                    if shutil.disk_usage(final.parent).free < continuation_floor:
                        space_failure = True
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                            process.wait(timeout=2)
                        except ProcessLookupError:
                            pass
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        break
                    if time.monotonic() >= next_progress_update:
                        partial_size = partial.stat().st_size if partial.exists() else 0
                        progress = min(0.8, 0.05 + 0.75 * partial_size / max(1, int(plan["estimatedBytes"])))
                        self.store.transition_job(job_id, "RUNNING", progress, "Rendering immutable plan")
                        next_progress_update = time.monotonic() + 1
                    time.sleep(0.2)
                process.wait()
                diagnostics.seek(0)
                stderr = diagnostics.read(200_000)
                with self.lock:
                    running = self.jobs.pop(job_id, running)
            if running.cancel_requested:
                _unlink_exact(partial)
                _unlink_exact(manifest_partial)
                self._finish_cancellation(job_id, artifact_id, "temporary outputs removed")
                return
            if space_failure:
                raise DomainError("INSUFFICIENT_SPACE", "Safe free-space threshold was reached during render")
            if process.returncode != 0:
                raise RuntimeError(f"Media engine exited with status {process.returncode}")
            self.store.transition_job(job_id, "RUNNING", 0.85, "Verifying sources and output")
            video_digest = full_digest(partial)
            manifest_value = build_v1_manifest(plan, {"id": artifact_id, "videoDigest": video_digest, "manifestDigest": None})
            manifest_partial.write_text(json.dumps(manifest_value, indent=2, sort_keys=True), encoding="utf-8")
            manifest_digest = full_digest(manifest_partial)
            if final.exists() or manifest_final.exists():
                raise DomainError("DESTINATION_EXISTS", "Output destination changed during render")
            if self._finalization_stopped(job_id, artifact, plan, final):
                _unlink_exact(partial)
                _unlink_exact(manifest_partial)
                self._finish_cancellation(job_id, artifact_id, "before artifact promotion")
                return
            _promote_no_replace(partial, final)
            video_promoted = True
            _unlink_exact(partial)
            if self._finalization_stopped(job_id, artifact, plan, final, revalidate_sources=False):
                _unlink_exact(manifest_partial)
                self._finish_cancellation(
                    job_id, artifact_id, "after video promotion", recoverable=True
                )
                return
            _promote_no_replace(manifest_partial, manifest_final)
            manifest_promoted = True
            _unlink_exact(manifest_partial)
            if self._finalization_stopped(job_id, artifact, plan, final, revalidate_sources=False):
                self._finish_cancellation(
                    job_id, artifact_id, "after manifest promotion", recoverable=True
                )
                return
            _verify_published_file(final, video_digest)
            _verify_published_file(manifest_final, manifest_digest)
            completed = self.store.complete_render_artifact(
                job_id,
                artifact_id,
                video_digest,
                manifest_digest,
                {"videoBytes": final.stat().st_size, "manifestBytes": manifest_final.stat().st_size},
            )
            if not completed:
                self._finish_cancellation(
                    job_id, artifact_id, "before durable completion", recoverable=True
                )
        except DomainError as error:
            _unlink_exact(partial)
            _unlink_exact(manifest_partial)
            artifact_status = "FAILED_RECOVERABLE" if video_promoted or manifest_promoted else "FAILED"
            self.store.update_artifact(artifact_id, status=artifact_status, details_json={"errorCode": error.code})
            self.store.transition_job(job_id, "FAILED", 0, str(error), error_code=error.code)
        except Exception as error:
            _unlink_exact(partial)
            _unlink_exact(manifest_partial)
            artifact_status = "FAILED_RECOVERABLE" if video_promoted or manifest_promoted else "FAILED"
            self.store.update_artifact(artifact_id, status=artifact_status, details_json={"error": type(error).__name__})
            self.store.transition_job(job_id, "FAILED", 0, str(error)[:500], error_code="INTERNAL_ERROR")

    def _finish_cancellation(
        self, job_id: str, artifact_id: str, detail: str, *, recoverable: bool = False
    ) -> None:
        job = self.store.job(job_id)
        if job.get("errorCode") == "GRANT_REQUIRED":
            self.store.update_artifact(
                artifact_id,
                status="FAILED_RECOVERABLE" if recoverable else "FAILED",
                details_json={"errorCode": "GRANT_REQUIRED", "recoverableOutput": recoverable},
            )
            self.store.transition_job(
                job_id,
                "FAILED",
                0,
                f"Directory grant revoked; {detail}",
                error_code="GRANT_REQUIRED",
            )
            return
        self.store.update_artifact(
            artifact_id,
            status="FAILED_RECOVERABLE" if recoverable else "CANCELED",
            details_json={"recoverableOutput": recoverable} if recoverable else {},
        )
        self.store.transition_job(job_id, "CANCELED", 0, f"Canceled; {detail}")

    def _finalization_stopped(
        self,
        job_id: str,
        artifact: dict[str, Any],
        plan: dict[str, Any],
        expected_final: Path,
        *,
        revalidate_sources: bool = True,
    ) -> bool:
        if self.store.job(job_id)["status"] == "CANCEL_REQUESTED":
            return True
        if revalidate_sources:
            self._validate_sources(plan)
        current_final = self.store.output_path(artifact["outputGrantId"], artifact["filename"])
        if current_final != expected_final:
            raise DomainError("PLAN_STALE", "Output destination changed during finalization")
        return False

    def _validate_sources(self, plan: dict[str, Any]) -> None:
        project = self.store.project(plan["projectId"])
        root = self.store.library_root(project["libraryId"])
        for expected in plan["sources"]:
            media = self.store.media_record(expected["assetId"])
            if media["relative_path"] != expected["libraryRelativePath"]:
                raise DomainError("SOURCE_CHANGED", "A selected source path changed after review")
            try:
                source = _safe_source(root, expected["libraryRelativePath"])
                stat = source.stat()
            except (OSError, PreflightError) as error:
                raise DomainError("SOURCE_CHANGED", "A selected source is no longer available") from error
            if stat.st_size != expected["size"] or full_digest(source) != expected["sha256"]:
                raise DomainError("SOURCE_CHANGED", "A selected source changed after review")

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.job(job_id)
        if job["status"] in {"CANCELED", "SUCCEEDED", "FAILED", "INTERRUPTED", "FAILED_RECOVERABLE"}:
            return job
        self.store.transition_job(job_id, "CANCEL_REQUESTED", job["progress"], "Cancellation requested")
        with self.lock:
            running = self.jobs.get(job_id)
            if running:
                self._request_process_stop(running)
        return self.store.job(job_id)

    def _request_process_stop(self, running: RunningJob, grace_seconds: float = 5) -> None:
        with self.lock:
            running.cancel_requested = True
            current = time.monotonic()
            if running.term_sent_at is None:
                running.term_sent_at = current
                try:
                    os.killpg(running.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                return
            if not running.kill_sent and current - running.term_sent_at >= grace_seconds:
                running.kill_sent = True
                try:
                    os.killpg(running.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def shutdown(self, timeout_seconds: float = 5) -> None:
        """Request cancellation and wait briefly for owned process trees to settle."""
        with self.lock:
            job_ids = list(self.reserved | self.jobs.keys())
        for job_id in job_ids:
            try:
                self.cancel(job_id)
            except DomainError:
                pass
        deadline = time.monotonic() + timeout_seconds
        for job_id in job_ids:
            while time.monotonic() < deadline:
                try:
                    if self.store.job(job_id)["status"] in {"CANCELED", "SUCCEEDED", "FAILED", "INTERRUPTED", "FAILED_RECOVERABLE"}:
                        break
                except DomainError:
                    break
                time.sleep(0.05)
        with self.lock:
            unsettled = list(self.jobs.items())
        for job_id, running in unsettled:
            if running.process.poll() is None:
                with self.lock:
                    running.term_sent_at = running.term_sent_at or time.monotonic() - timeout_seconds
                self._request_process_stop(running, grace_seconds=0)
        final_deadline = time.monotonic() + 1
        for job_id, _running in unsettled:
            while time.monotonic() < final_deadline:
                if self.store.job(job_id)["status"] in {
                    "CANCELED", "SUCCEEDED", "FAILED", "INTERRUPTED", "FAILED_RECOVERABLE"
                }:
                    break
                time.sleep(0.05)
            else:
                job = self.store.job(job_id)
                if job["status"] not in {"CANCELED", "SUCCEEDED", "FAILED", "INTERRUPTED", "FAILED_RECOVERABLE"}:
                    try:
                        self.store.transition_job(
                            job_id,
                            "FAILED_RECOVERABLE",
                            job["progress"],
                            "Render process was terminated during shutdown",
                        )
                    except DomainError:
                        pass

    def reconcile_artifacts(self) -> None:
        for artifact in self.store.artifacts(incomplete_only=True):
            try:
                final = self.store.output_path(artifact["outputGrantId"], artifact["filename"])
            except DomainError:
                self.store.update_artifact(artifact["id"], status="FAILED_RECOVERABLE", details_json={"reason": "grant unavailable"})
                continue
            manifest = final.with_name(artifact["manifestFilename"])
            token = artifact["id"].rsplit("_", 1)[-1]
            partials = [
                final.with_name(f".{final.stem}.partial.{token}{final.suffix}"),
                final.with_name(f".{manifest.name}.partial.{token}"),
            ]
            quarantined: list[str] = []
            quarantine_failures: list[dict[str, Any]] = []
            for partial in partials:
                if not partial.exists():
                    continue
                recovery = self.store.path.parent / "recovery"
                recovery.mkdir(parents=True, exist_ok=True)
                destination = recovery / f"{artifact['id']}-{partial.name.lstrip('.')}"
                try:
                    self._quarantine_partial(partial, destination)
                    quarantined.append(destination.name)
                except OSError as error:
                    quarantine_failures.append(
                        {"filename": partial.name, "error": type(error).__name__, "errno": error.errno}
                    )
            if final.exists() or manifest.exists():
                self.store.update_artifact(
                    artifact["id"],
                    status="FAILED_RECOVERABLE",
                    details_json={
                        "videoPresent": final.exists(),
                        "manifestPresent": manifest.exists(),
                        "quarantinedTemporaryFiles": quarantined,
                        "quarantineFailures": quarantine_failures,
                    },
                )
            elif quarantined or quarantine_failures:
                self.store.update_artifact(
                    artifact["id"],
                    status="FAILED_RECOVERABLE",
                    details_json={
                        "quarantinedTemporaryFiles": quarantined,
                        "quarantineFailures": quarantine_failures,
                    },
                )

    @staticmethod
    def _quarantine_partial(partial: Path, destination: Path) -> None:
        try:
            os.replace(partial, destination)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            shutil.move(str(partial), str(destination))


def _estimate_output_bytes(duration_us: int, profile: str) -> int:
    seconds = max(1, duration_us / 1_000_000)
    bits_per_second = 40_000_000 if profile == "ARCHIVAL_LOSSLESS" else 10_000_000
    return int(seconds * bits_per_second / 8)


@lru_cache(maxsize=4)
def _tool_version(tool: str) -> str:
    try:
        result = subprocess.run(
            [tool, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3,
            check=False,
        )
        return (result.stdout.splitlines()[0] if result.stdout else f"{tool}/unknown")[:300]
    except (OSError, subprocess.TimeoutExpired):
        return f"{tool}/unavailable"


def _seconds(value_us: int) -> str:
    return f"{value_us / 1_000_000:.6f}"


def _unlink_exact(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _promote_no_replace(partial: Path, final: Path) -> None:
    """Atomically publish one same-filesystem output without replacing a peer file.

    Canonical render partials and their final names are siblings. A hard link
    therefore gives us an atomic create-if-absent operation on filesystems that
    support safe local promotion. Unsupported filesystems fail closed instead
    of exposing a partially copied final or falling back to overwrite behavior.
    """

    try:
        os.link(partial, final, follow_symlinks=False)
    except FileExistsError as error:
        raise DomainError("DESTINATION_EXISTS", "Output destination changed during render") from error
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise DomainError("DESTINATION_EXISTS", "Output destination changed during render") from error
        raise DomainError(
            "INTERNAL_ERROR",
            "Output filesystem does not support safe exclusive artifact promotion",
            {"errno": error.errno},
        ) from error


def _verify_published_file(path: Path, expected_digest: str) -> None:
    try:
        actual_digest = full_digest(path)
    except OSError as error:
        raise DomainError(
            "DESTINATION_EXISTS", "Published output changed during finalization"
        ) from error
    if actual_digest != expected_digest:
        raise DomainError("DESTINATION_EXISTS", "Published output changed during finalization")
