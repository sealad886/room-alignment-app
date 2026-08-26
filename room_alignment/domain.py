from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any


MAX_RATE_PPM = 2_000
AUDIO_MODES = {"FOLLOW_VIDEO", "FIXED_SOURCE", "FIXED_CLIP", "SILENCE"}
ANCHOR_MODES = {"PROGRAM_TIME", "SOURCE_TIME"}
ALIGNMENT_STATES = {"PROVISIONAL", "ACCEPTED", "REVIEW_REQUIRED", "UNRESOLVED"}
TIMELINE_SECTION_MODES = {"KEEP", "EXCLUDE", "SLATE"}
BLOCKING = "BLOCKING"
WARNING = "WARNING"
COMMAND_PAYLOAD_FIELDS = {
    "UpdateProjectMetadata": {"name"},
    "AddLogicalSource": {"id", "label"},
    "RenameLogicalSource": {"sourceId", "label"},
    "MergeLogicalSources": {"targetSourceId", "sourceIds"},
    "SplitLogicalSource": {"sourceId", "newSourceId", "clipIds", "label"},
    "ArchiveLogicalSource": {"sourceId", "archived"},
    "AssignClip": {"clipId", "logicalSourceId"},
    "SetReferenceSource": {"sourceId"},
    "SetSyncTransform": {"clipId", "sync", "confirmDrift"},
    "SetClipAlignment": {"clipId", "alignment", "confirmDrift"},
    "InitializeProgram": set(),
    "SetTimelineSections": {"sections"},
    "GenerateProgramDraft": {"alignmentDigest", "selectionDigest", "replaceExisting"},
    "AddVideoBlock": {"id", "startUs", "endUs", "logicalSourceId", "pinnedClipId"},
    "SplitVideoBlock": {"blockId", "atUs", "newBlockId"},
    "MoveVideoBoundary": {"leftBlockId", "rightBlockId", "atUs"},
    "DeleteVideoBlock": {"blockId"},
    "AssignVideoSource": {"blockId", "logicalSourceId"},
    "PinVideoClip": {"blockId", "clipId"},
    "CutToSource": {"blockId", "atUs", "logicalSourceId", "pinnedClipId", "newBlockId"},
    "AddAudioBlock": {
        "id", "startUs", "endUs", "mode", "logicalSourceId", "clipId", "offsetUs", "ratePpm",
        "confirmDrift"
    },
    "SplitAudioBlock": {"blockId", "atUs"},
    "MoveAudioBoundary": {"leftBlockId", "rightBlockId", "atUs"},
    "DeleteAudioBlock": {"blockId"},
    "SetAudioMode": {
        "blockId", "mode", "logicalSourceId", "clipId", "offsetUs", "ratePpm", "confirmDrift"
    },
    "SetAnchoringMode": {"anchorMode"},
    "ReconcileBoundary": {"operation", "leftBlockId", "rightBlockId", "atUs", "startUs", "endUs"},
    "ArchiveProject": set(),
    "AcceptAlignmentSuggestion": {"suggestionId", "clipId", "sync", "confirmDrift"},
    "AcceptAlignmentSuggestions": {"suggestions"},
    "RejectAlignmentSuggestion": {"suggestionId"},
}


class DomainError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def seconds_to_us(value: int | float | str | Decimal) -> int:
    return int((Decimal(str(value)) * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _asset_duration_us(asset: dict[str, Any]) -> int:
    duration_us = asset.get("durationUs")
    if duration_us is not None:
        return int(duration_us)
    duration = asset.get("duration")
    return seconds_to_us(duration) if duration is not None else 0


@dataclass(frozen=True, slots=True)
class SyncTransform:
    anchor_source_us: int = 0
    anchor_output_us: int = 0
    rate_ppm: int = 0

    def __post_init__(self) -> None:
        if abs(self.rate_ppm) > MAX_RATE_PPM:
            raise DomainError(
                "VALIDATION_FAILED",
                f"ratePpm must be between {-MAX_RATE_PPM} and {MAX_RATE_PPM}",
            )

    @property
    def numerator(self) -> int:
        return 1_000_000 + self.rate_ppm

    def source_to_output(self, source_us: int) -> int:
        delta = source_us - self.anchor_source_us
        scaled = _round_ratio(delta * self.numerator, 1_000_000)
        return self.anchor_output_us + scaled

    def output_to_source(self, output_us: int) -> int:
        delta = output_us - self.anchor_output_us
        scaled = _round_ratio(delta * 1_000_000, self.numerator)
        return self.anchor_source_us + scaled

    def to_dict(self) -> dict[str, int]:
        return {
            "anchorSourceUs": self.anchor_source_us,
            "anchorOutputUs": self.anchor_output_us,
            "ratePpm": self.rate_ppm,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> SyncTransform:
        value = value or {}
        return cls(
            int(value.get("anchorSourceUs", 0)),
            int(value.get("anchorOutputUs", 0)),
            int(value.get("ratePpm", 0)),
        )


@dataclass(frozen=True, slots=True)
class ClipAlignmentTransform:
    """Map source-relative time onto the shared evidence clock.

    ``SyncTransform.anchorOutputUs`` remains readable for legacy projects, but
    new project state uses ``anchorAlignedUs`` so evidence time cannot be
    mistaken for the final edited program clock.
    """

    anchor_source_us: int = 0
    anchor_aligned_us: int = 0
    rate_ppm: int = 0

    def __post_init__(self) -> None:
        if abs(self.rate_ppm) > MAX_RATE_PPM:
            raise DomainError(
                "VALIDATION_FAILED",
                f"ratePpm must be between {-MAX_RATE_PPM} and {MAX_RATE_PPM}",
            )

    @property
    def numerator(self) -> int:
        return 1_000_000 + self.rate_ppm

    def source_to_aligned(self, source_us: int) -> int:
        delta = source_us - self.anchor_source_us
        return self.anchor_aligned_us + _round_ratio(delta * self.numerator, 1_000_000)

    def aligned_to_source(self, aligned_us: int) -> int:
        delta = aligned_us - self.anchor_aligned_us
        return self.anchor_source_us + _round_ratio(delta * 1_000_000, self.numerator)

    # Transitional aliases keep the media compiler compatible while it is
    # moved to explicit aligned/program clock composition in MS-5.
    def source_to_output(self, source_us: int) -> int:
        return self.source_to_aligned(source_us)

    def output_to_source(self, output_us: int) -> int:
        return self.aligned_to_source(output_us)

    def to_legacy_sync_dict(self) -> dict[str, int]:
        return {
            "anchorSourceUs": self.anchor_source_us,
            "anchorOutputUs": self.anchor_aligned_us,
            "ratePpm": self.rate_ppm,
        }

    def to_dict(self) -> dict[str, int]:
        return {
            "anchorSourceUs": self.anchor_source_us,
            "anchorAlignedUs": self.anchor_aligned_us,
            "ratePpm": self.rate_ppm,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> ClipAlignmentTransform:
        value = value or {}
        return cls(
            int(value.get("anchorSourceUs", 0)),
            int(value.get("anchorAlignedUs", value.get("anchorOutputUs", 0))),
            int(value.get("ratePpm", 0)),
        )

    @classmethod
    def from_legacy_sync(cls, value: dict[str, Any] | None) -> ClipAlignmentTransform:
        legacy = SyncTransform.from_dict(value)
        return cls(legacy.anchor_source_us, legacy.anchor_output_us, legacy.rate_ppm)


def _round_ratio(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise DomainError("VALIDATION_FAILED", "Time transform denominator must be positive")
    sign = -1 if numerator < 0 else 1
    absolute = abs(numerator)
    quotient, remainder = divmod(absolute, denominator)
    twice = remainder * 2
    if twice > denominator or (twice == denominator and quotient % 2):
        quotient += 1
    return sign * quotient


def new_project(
    name: str,
    library_id: str,
    assets: Iterable[dict[str, Any]],
    project_id: str | None = None,
    source_groups: Iterable[dict[str, Any]] | None = None,
    *,
    selection_snapshot: dict[str, Any] | None = None,
    initialize_legacy_program: bool = True,
) -> dict[str, Any]:
    chosen = list(assets)
    if not chosen:
        raise DomainError("VALIDATION_FAILED", "Project requires at least one media asset")
    created = now_iso()
    assets_by_id = {str(asset["id"]): asset for asset in chosen}
    groups = list(source_groups or [])
    if source_groups is not None and not groups:
        raise DomainError("VALIDATION_FAILED", "Confirmed source groups may not be empty")
    if groups:
        grouped_ids = [str(asset_id) for group in groups for asset_id in group.get("assetIds", [])]
        if len(grouped_ids) != len(set(grouped_ids)) or set(grouped_ids) != set(assets_by_id):
            raise DomainError(
                "VALIDATION_FAILED",
                "Confirmed source groups must contain every selected asset exactly once",
            )
    else:
        groups = [
            {"label": asset.get("camera") or f"Source {index + 1}", "assetIds": [asset["id"]]}
            for index, asset in enumerate(chosen)
        ]
    captured_us = {
        str(asset["id"]): value
        for asset in chosen
        if (value := _confirmed_timestamp_us(asset.get("captured_at"))) is not None
    }
    evidence_origin_us = min(captured_us.values(), default=0)
    sources: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []
    for group in groups:
        group_assets = [assets_by_id[str(asset_id)] for asset_id in group["assetIds"]]
        candidate_keys = sorted(
            {str(asset.get("sourceCandidateId") or asset.get("camera") or asset["id"]) for asset in group_assets}
        )
        source_id = opaque_id("src")
        sources.append(
            {
                "id": source_id,
                "label": str(group.get("label") or group_assets[0].get("camera") or f"Source {len(sources) + 1}"),
                "reference": not sources,
                "archived": False,
                "candidateKey": candidate_keys[0] if len(candidate_keys) == 1 else digest_json(candidate_keys),
                "identityState": "USER_CONFIRMED" if source_groups is not None else "USER_REVIEW_REQUIRED",
            }
        )
        for asset in group_assets:
            clip = {
                "id": opaque_id("clip"),
                "assetId": asset["id"],
                "logicalSourceId": source_id,
            }
            if initialize_legacy_program:
                clip["sync"] = SyncTransform().to_dict()
            else:
                captured = captured_us.get(str(asset["id"]))
                state = "PROVISIONAL" if captured is not None else "UNRESOLVED"
                clip.update(
                    {
                        "alignment": ClipAlignmentTransform(
                            anchor_aligned_us=(captured - evidence_origin_us) if captured is not None else 0
                        ).to_dict(),
                        "alignmentState": state,
                        "alignmentConfidence": 0.6 if captured is not None else 0.0,
                        "alignmentEvidence": ["timestamp"] if captured is not None else [],
                    }
                )
            clips.append(clip)
    exact_asset_ids = [str(asset["id"]) for asset in chosen]
    snapshot = copy.deepcopy(selection_snapshot) if selection_snapshot is not None else {
        "clusterGenerationId": None,
        "selectedSessionIds": [],
        "selectedEventIds": [],
        "assetIds": exact_asset_ids,
        "manualIncludeAssetIds": exact_asset_ids,
        "manualExcludeAssetIds": [],
    }
    snapshot["assetIds"] = exact_asset_ids
    snapshot["digest"] = digest_json({key: value for key, value in snapshot.items() if key != "digest"})
    project = {
        "id": project_id or opaque_id("project"),
        "name": name.strip() or "Untitled alignment",
        "libraryId": library_id,
        "revision": 1,
        "provenanceRevision": 0,
        "archived": False,
        "anchorMode": "PROGRAM_TIME",
        "logicalSources": sources,
        "clips": clips,
        "selectionSnapshot": snapshot,
        "alignmentDigest": "",
        "timelineSections": [],
        "programDraft": None,
        "videoBlocks": [],
        "audioBlocks": [],
        "renderSettings": {"profile": "COMPATIBLE"},
        "review": None,
        "createdAt": created,
        "updatedAt": created,
    }
    project["alignmentDigest"] = alignment_digest(project)
    if initialize_legacy_program:
        return initialize_program(project, {asset["id"]: asset for asset in chosen})
    return project


def _confirmed_timestamp_us(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = parsed.astimezone(UTC) - epoch
    return ((delta.days * 86_400) + delta.seconds) * 1_000_000 + delta.microseconds


def _clip_alignment(clip: dict[str, Any]) -> ClipAlignmentTransform:
    if isinstance(clip.get("alignment"), dict):
        return ClipAlignmentTransform.from_dict(clip["alignment"])
    return ClipAlignmentTransform.from_legacy_sync(clip.get("sync"))


def alignment_digest(project: dict[str, Any]) -> str:
    return digest_json(
        [
            {
                "clipId": clip["id"],
                "assetId": clip["assetId"],
                "logicalSourceId": clip["logicalSourceId"],
                "alignment": _clip_alignment(clip).to_dict(),
                "state": clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED"),
                "confidence": float(clip.get("alignmentConfidence", 1.0 if "sync" in clip else 0.0)),
            }
            for clip in sorted(project.get("clips", []), key=lambda item: item["id"])
        ]
    )


def aligned_extent(
    project: dict[str, Any], assets: dict[str, dict[str, Any]], *, include_provisional: bool = True
) -> dict[str, int]:
    ranges = _clip_ranges(project, assets, include_provisional=include_provisional)
    if not ranges:
        return {"startAlignedUs": 0, "endAlignedUs": 0, "durationUs": 0}
    start_us = min(int(item["startUs"]) for item in ranges)
    end_us = max(int(item["endUs"]) for item in ranges)
    return {"startAlignedUs": start_us, "endAlignedUs": end_us, "durationUs": end_us - start_us}


def initialize_program(project: dict[str, Any], assets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(project)
    clip_ranges = _clip_ranges(result, assets)
    if not clip_ranges:
        return result
    reference_ids = {
        source["id"] for source in result["logicalSources"] if source.get("reference") and not source.get("archived")
    }
    boundaries = sorted({point for item in clip_ranges for point in (item["startUs"], item["endUs"])})
    blocks: list[dict[str, Any]] = []
    for start_us, end_us in zip(boundaries, boundaries[1:]):
        if end_us <= start_us:
            continue
        covering = [item for item in clip_ranges if item["startUs"] <= start_us and item["endUs"] >= end_us]
        if not covering:
            continue
        preferred = [item for item in covering if item["logicalSourceId"] in reference_ids]
        selected = sorted(preferred or covering, key=lambda item: (item["logicalSourceId"], item["clipId"]))[0]
        if blocks and blocks[-1]["endUs"] == start_us and blocks[-1]["logicalSourceId"] == selected["logicalSourceId"]:
            blocks[-1]["endUs"] = end_us
        else:
            blocks.append(
                {
                    "id": opaque_id("vblock"),
                    "startUs": start_us,
                    "endUs": end_us,
                    "logicalSourceId": selected["logicalSourceId"],
                    "pinnedClipId": None,
                }
            )
    result["videoBlocks"] = blocks
    if blocks:
        result["audioBlocks"] = [
            {
                "id": opaque_id("ablock"),
                "startUs": blocks[0]["startUs"],
                "endUs": blocks[-1]["endUs"],
                "mode": "FOLLOW_VIDEO",
                "logicalSourceId": None,
                "clipId": None,
                "offsetUs": 0,
                "ratePpm": 0,
            }
        ]
    return result


def validate_project(project: dict[str, Any]) -> None:
    required = {
        "id",
        "name",
        "libraryId",
        "revision",
        "logicalSources",
        "clips",
        "videoBlocks",
        "audioBlocks",
        "anchorMode",
    }
    missing = sorted(required - project.keys())
    if missing:
        raise DomainError("VALIDATION_FAILED", f"Project missing fields: {', '.join(missing)}")
    if project["anchorMode"] not in ANCHOR_MODES:
        raise DomainError("VALIDATION_FAILED", "Unknown anchorMode")
    _unique_ids(project["logicalSources"], "logical source")
    _unique_ids(project["clips"], "project clip")
    _unique_ids(project["videoBlocks"], "video block")
    _unique_ids(project["audioBlocks"], "audio block")
    source_ids = {item["id"] for item in project["logicalSources"]}
    clip_ids = {item["id"] for item in project["clips"]}
    for clip in project["clips"]:
        if clip.get("logicalSourceId") not in source_ids:
            raise DomainError("VALIDATION_FAILED", f"Clip {clip['id']} references an unknown logical source")
        _clip_alignment(clip)
        state = clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED")
        if state not in ALIGNMENT_STATES:
            raise DomainError("VALIDATION_FAILED", f"Clip {clip['id']} has an invalid alignment state")
    for section in project.get("timelineSections", []):
        if section.get("mode") not in TIMELINE_SECTION_MODES:
            raise DomainError("VALIDATION_FAILED", f"Timeline section {section.get('id')} has an invalid mode")
        if int(section.get("endAlignedUs", 0)) <= int(section.get("startAlignedUs", 0)):
            raise DomainError("VALIDATION_FAILED", "Timeline sections must have positive aligned duration")
    for block in project["videoBlocks"]:
        _validate_interval(block)
        if block.get("logicalSourceId") not in source_ids:
            raise DomainError("VALIDATION_FAILED", f"Video block {block['id']} references an unknown source")
        if block.get("pinnedClipId") is not None and block["pinnedClipId"] not in clip_ids:
            raise DomainError("VALIDATION_FAILED", f"Video block {block['id']} pins an unknown clip")
    for block in project["audioBlocks"]:
        _validate_interval(block)
        if block.get("mode") not in AUDIO_MODES:
            raise DomainError("VALIDATION_FAILED", f"Audio block {block['id']} has an invalid mode")
        if block.get("mode") == "FIXED_SOURCE" and block.get("logicalSourceId") not in source_ids:
            raise DomainError("VALIDATION_FAILED", f"Audio block {block['id']} requires a logical source")
        if block.get("mode") == "FIXED_CLIP" and block.get("clipId") not in clip_ids:
            raise DomainError("VALIDATION_FAILED", f"Audio block {block['id']} requires a project clip")
        if abs(int(block.get("ratePpm", 0))) > MAX_RATE_PPM:
            raise DomainError("VALIDATION_FAILED", f"Audio block {block['id']} rate exceeds safe bounds")


def compile_program(project: dict[str, Any], assets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    validate_project(project)
    video_blocks = sorted(project["videoBlocks"], key=lambda item: (int(item["startUs"]), item["id"]))
    audio_blocks = sorted(project["audioBlocks"], key=lambda item: (int(item["startUs"]), item["id"]))
    issues: list[dict[str, Any]] = []
    duration_us = max((int(item["endUs"]) for item in video_blocks), default=0)
    if not video_blocks or duration_us <= 0:
        issues.append(_issue("VIDEO_GAP", 0, 0, "A positive-duration video program is required"))
    _interval_issues(video_blocks, duration_us, "VIDEO", issues)
    video_slices: list[dict[str, Any]] = []
    for block in video_blocks:
        video_slices.extend(_compile_source_block(project, assets, block, issues, require_audio=False))
    _interval_issues(audio_blocks, duration_us, "AUDIO", issues)
    audio_slices: list[dict[str, Any]] = []
    for block in audio_blocks:
        audio_slices.extend(_compile_audio_block(project, assets, block, video_slices, issues))
    issues = _dedupe_issues(issues)
    return {
        "projectId": project["id"],
        "revision": project["revision"],
        "durationUs": duration_us,
        "videoSlices": sorted(video_slices, key=lambda item: (item["startUs"], item["id"])),
        "audioSlices": sorted(audio_slices, key=lambda item: (item["startUs"], item["id"])),
        "issues": issues,
        "valid": not any(item["severity"] == BLOCKING for item in issues),
    }


def program_at(compiled: dict[str, Any], output_us: int) -> dict[str, Any]:
    return {
        "outputUs": output_us,
        "video": next(
            (item for item in compiled["videoSlices"] if item["startUs"] <= output_us < item["endUs"]),
            None,
        ),
        "audio": next(
            (item for item in compiled["audioSlices"] if item["startUs"] <= output_us < item["endUs"]),
            None,
        ),
        "issues": [
            item
            for item in compiled["issues"]
            if item.get("startUs", 0) <= output_us < item.get("endUs", compiled["durationUs"] + 1)
        ],
    }


def apply_command(
    project: dict[str, Any], command_type: str, payload: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    result = copy.deepcopy(project)
    handlers = {
        "UpdateProjectMetadata": _update_metadata,
        "AddLogicalSource": _add_source,
        "RenameLogicalSource": _rename_source,
        "MergeLogicalSources": _merge_sources,
        "SplitLogicalSource": _split_source,
        "ArchiveLogicalSource": _archive_source,
        "AssignClip": _assign_clip,
        "SetReferenceSource": _set_reference,
        "SetSyncTransform": lambda p, command_payload: _set_sync(p, command_payload, assets),
        "SetClipAlignment": lambda p, command_payload: _set_clip_alignment(
            p, command_payload, assets
        ),
        "InitializeProgram": lambda p, _payload: _replace(p, initialize_program(p, assets)),
        "SetTimelineSections": _set_timeline_sections,
        "GenerateProgramDraft": lambda p, command_payload: _replace(
            p, generate_program_draft(p, assets, command_payload)
        ),
        "AddVideoBlock": _add_video_block,
        "SplitVideoBlock": _split_video_block,
        "MoveVideoBoundary": _move_video_boundary,
        "DeleteVideoBlock": _delete_video_block,
        "AssignVideoSource": _assign_video_source,
        "PinVideoClip": _pin_video_clip,
        "CutToSource": _cut_to_source,
        "AddAudioBlock": _add_audio_block,
        "SplitAudioBlock": _split_audio_block,
        "MoveAudioBoundary": _move_audio_boundary,
        "DeleteAudioBlock": _delete_audio_block,
        "SetAudioMode": _set_audio_mode,
        "SetAnchoringMode": _set_anchor_mode,
        "ReconcileBoundary": _reconcile_boundary,
        "ArchiveProject": lambda p, _payload: p.update(archived=True),
        "AcceptAlignmentSuggestion": lambda p, command_payload: _accept_suggestion(
            p, command_payload, assets
        ),
        "AcceptAlignmentSuggestions": lambda p, command_payload: _accept_suggestions(
            p, command_payload, assets
        ),
        "RejectAlignmentSuggestion": lambda _p, _payload: None,
    }
    handler = handlers.get(command_type)
    if handler is None:
        raise DomainError("VALIDATION_FAILED", f"Unsupported commandType: {command_type}")
    if set(payload) - COMMAND_PAYLOAD_FIELDS[command_type]:
        raise DomainError("VALIDATION_FAILED", "Command payload contains unknown fields")
    try:
        handler(result, payload)
    except DomainError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise DomainError("VALIDATION_FAILED", "Command payload is invalid") from error
    result["review"] = None
    result["updatedAt"] = now_iso()
    result["alignmentDigest"] = alignment_digest(result)
    validate_project(result)
    return result


def _accept_suggestions(
    project: dict[str, Any], payload: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> None:
    suggestions = payload.get("suggestions")
    if not isinstance(suggestions, list) or not suggestions:
        raise DomainError("VALIDATION_FAILED", "suggestions must be a non-empty list")
    seen: set[str] = set()
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            raise DomainError("VALIDATION_FAILED", "Every accepted suggestion must be an object")
        suggestion_id = str(suggestion.get("suggestionId", ""))
        if not suggestion_id or suggestion_id in seen:
            raise DomainError("VALIDATION_FAILED", "Suggestion IDs must be present and unique")
        seen.add(suggestion_id)
        _accept_suggestion(project, suggestion, assets)


def _replace(target: dict[str, Any], source: dict[str, Any]) -> None:
    target.clear()
    target.update(source)


def _update_metadata(project: dict[str, Any], payload: dict[str, Any]) -> None:
    if "name" in payload:
        name = str(payload["name"]).strip()
        if not name:
            raise DomainError("VALIDATION_FAILED", "Project name cannot be empty")
        project["name"] = name[:200]


def _add_source(project: dict[str, Any], payload: dict[str, Any]) -> None:
    project["logicalSources"].append(
        {
            "id": payload.get("id") or opaque_id("src"),
            "label": str(payload.get("label") or "New source")[:200],
            "reference": False,
            "archived": False,
        }
    )


def _rename_source(project: dict[str, Any], payload: dict[str, Any]) -> None:
    source = _find(project["logicalSources"], payload["sourceId"], "logical source")
    source["label"] = str(payload["label"]).strip()[:200]
    if not source["label"]:
        raise DomainError("VALIDATION_FAILED", "Source label cannot be empty")


def _merge_sources(project: dict[str, Any], payload: dict[str, Any]) -> None:
    target = payload["targetSourceId"]
    merged = set(payload.get("sourceIds", [])) - {target}
    target_source = _find(project["logicalSources"], target, "logical source")
    target_source["reference"] = bool(target_source.get("reference")) or any(
        bool(source.get("reference")) for source in project["logicalSources"] if source["id"] in merged
    )
    for clip in project["clips"]:
        if clip["logicalSourceId"] in merged:
            clip["logicalSourceId"] = target
    for block in project["videoBlocks"] + project["audioBlocks"]:
        if block.get("logicalSourceId") in merged:
            block["logicalSourceId"] = target
    project["logicalSources"] = [item for item in project["logicalSources"] if item["id"] not in merged]


def _split_source(project: dict[str, Any], payload: dict[str, Any]) -> None:
    source_id = payload["sourceId"]
    _find(project["logicalSources"], source_id, "logical source")
    clip_ids = set(payload.get("clipIds", []))
    if not clip_ids:
        raise DomainError("VALIDATION_FAILED", "Source split requires at least one clip")
    selected = [clip for clip in project["clips"] if clip["id"] in clip_ids]
    if len(selected) != len(clip_ids) or any(clip["logicalSourceId"] != source_id for clip in selected):
        raise DomainError("VALIDATION_FAILED", "Every split clip must belong to the selected source")
    _add_source(project, {"id": payload.get("newSourceId"), "label": payload.get("label", "Split source")})
    new_id = project["logicalSources"][-1]["id"]
    for clip in selected:
        clip["logicalSourceId"] = new_id


def _archive_source(project: dict[str, Any], payload: dict[str, Any]) -> None:
    _find(project["logicalSources"], payload["sourceId"], "logical source")["archived"] = bool(
        payload.get("archived", True)
    )


def _assign_clip(project: dict[str, Any], payload: dict[str, Any]) -> None:
    _find(project["logicalSources"], payload["logicalSourceId"], "logical source")
    _find(project["clips"], payload["clipId"], "project clip")["logicalSourceId"] = payload[
        "logicalSourceId"
    ]


def _set_reference(project: dict[str, Any], payload: dict[str, Any]) -> None:
    _find(project["logicalSources"], payload["sourceId"], "logical source")
    for source in project["logicalSources"]:
        source["reference"] = source["id"] == payload["sourceId"]


def _set_sync(
    project: dict[str, Any], payload: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> None:
    _set_alignment(
        project,
        payload["clipId"],
        ClipAlignmentTransform.from_legacy_sync(payload.get("sync")),
        bool(payload.get("confirmDrift")),
        assets,
        legacy_payload=True,
    )


def _set_clip_alignment(
    project: dict[str, Any], payload: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> None:
    _set_alignment(
        project,
        payload["clipId"],
        ClipAlignmentTransform.from_dict(payload.get("alignment")),
        bool(payload.get("confirmDrift")),
        assets,
        legacy_payload=False,
    )


def _set_alignment(
    project: dict[str, Any],
    clip_id: str,
    new: ClipAlignmentTransform,
    confirm_drift: bool,
    assets: dict[str, dict[str, Any]],
    *,
    legacy_payload: bool,
) -> None:
    clip = _find(project["clips"], clip_id, "project clip")
    old = _clip_alignment(clip)
    if new.rate_ppm and not confirm_drift:
        raise DomainError("VALIDATION_FAILED", "Non-zero ratePpm requires confirmDrift")
    if project["anchorMode"] == "SOURCE_TIME":
        original_video = copy.deepcopy(project["videoBlocks"])
        clip_ranges = _clip_ranges(project, assets)

        def remap(value_us: int) -> int:
            return new.source_to_output(old.output_to_source(int(value_us)))

        for block in project["videoBlocks"]:
            if _video_boundary_uses_clip(clip_ranges, block, "startUs", clip):
                block["startUs"] = remap(block["startUs"])
            if _video_boundary_uses_clip(clip_ranges, block, "endUs", clip):
                block["endUs"] = remap(block["endUs"])
        for block in project["audioBlocks"]:
            if block["mode"] == "SILENCE":
                continue
            if _audio_boundary_uses_clip(clip_ranges, original_video, block, "startUs", clip):
                block["startUs"] = remap(block["startUs"])
            if _audio_boundary_uses_clip(clip_ranges, original_video, block, "endUs", clip):
                block["endUs"] = remap(block["endUs"])
    if legacy_payload and "alignment" not in clip:
        clip["sync"] = new.to_legacy_sync_dict()
    else:
        clip.pop("sync", None)
        clip["alignment"] = new.to_dict()
        clip["alignmentState"] = "ACCEPTED"
        clip["alignmentConfidence"] = 1.0
        clip["alignmentEvidence"] = ["manual"]


def _set_timeline_sections(project: dict[str, Any], payload: dict[str, Any]) -> None:
    raw = payload.get("sections")
    if not isinstance(raw, list) or not raw:
        raise DomainError("VALIDATION_FAILED", "sections must be a non-empty array")
    sections: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise DomainError("VALIDATION_FAILED", "Every timeline section must be an object")
        mode = str(item.get("mode", ""))
        if mode not in TIMELINE_SECTION_MODES:
            raise DomainError("VALIDATION_FAILED", "Unknown timeline section mode")
        start_us = int(item.get("startAlignedUs", -1))
        end_us = int(item.get("endAlignedUs", -1))
        if start_us < 0 or end_us <= start_us:
            raise DomainError("VALIDATION_FAILED", "Timeline sections require a positive aligned interval")
        sections.append(
            {
                "id": str(item.get("id") or opaque_id("section")),
                "startAlignedUs": start_us,
                "endAlignedUs": end_us,
                "mode": mode,
                "slateText": "No recorded footage" if mode == "SLATE" else None,
            }
        )
    sections.sort(key=lambda item: (item["startAlignedUs"], item["id"]))
    for left, right in zip(sections, sections[1:]):
        if int(left["endAlignedUs"]) > int(right["startAlignedUs"]):
            raise DomainError("VALIDATION_FAILED", "Timeline sections may not overlap")
    project["timelineSections"] = sections


def generate_program_draft(
    project: dict[str, Any], assets: dict[str, dict[str, Any]], payload: dict[str, Any]
) -> dict[str, Any]:
    if str(payload.get("alignmentDigest", "")) != alignment_digest(project):
        raise DomainError("PLAN_STALE", "Alignment changed after the program draft was prepared")
    selection = project.get("selectionSnapshot") or {}
    if str(payload.get("selectionDigest", "")) != str(selection.get("digest", "")):
        raise DomainError("PLAN_STALE", "Project selection changed after the program draft was prepared")
    if project.get("videoBlocks") and not payload.get("replaceExisting"):
        raise DomainError("VALIDATION_FAILED", "replaceExisting is required to replace an existing program")
    unresolved = [
        clip["id"]
        for clip in project.get("clips", [])
        if clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED") != "ACCEPTED"
    ]
    if unresolved:
        raise DomainError(
            "COVERAGE_INVALID",
            "Every required clip must have accepted timing before a first cut is generated",
            {"unresolvedClipIds": unresolved},
        )
    result = initialize_program(project, assets)
    result["programDraft"] = {
        "id": opaque_id("draft"),
        "selectionDigest": selection.get("digest"),
        "alignmentDigest": alignment_digest(project),
        "generatedAt": now_iso(),
        "strategy": "coverage-continuity-v1",
    }
    return result


def _video_boundary_uses_clip(
    clip_ranges: list[dict[str, Any]],
    block: dict[str, Any],
    boundary: str,
    clip: dict[str, Any],
) -> bool:
    if block.get("logicalSourceId") != clip["logicalSourceId"]:
        return False
    pinned = block.get("pinnedClipId")
    if pinned is not None:
        return pinned == clip["id"]
    output_us = int(block[boundary])
    candidates = [
        item
        for item in clip_ranges
        if item["logicalSourceId"] == block["logicalSourceId"]
        and (
            item["startUs"] <= output_us < item["endUs"]
            if boundary == "startUs"
            else item["startUs"] < output_us <= item["endUs"]
        )
    ]
    return len(candidates) == 1 and candidates[0]["clipId"] == clip["id"]


def _audio_boundary_uses_clip(
    clip_ranges: list[dict[str, Any]],
    original_video: list[dict[str, Any]],
    block: dict[str, Any],
    boundary: str,
    clip: dict[str, Any],
) -> bool:
    if block["mode"] == "FIXED_CLIP":
        return block.get("clipId") == clip["id"]
    if block["mode"] == "FIXED_SOURCE":
        synthetic = {
            "startUs": block["startUs"],
            "endUs": block["endUs"],
            "logicalSourceId": block.get("logicalSourceId"),
            "pinnedClipId": None,
        }
        return _video_boundary_uses_clip(clip_ranges, synthetic, boundary, clip)
    if block["mode"] != "FOLLOW_VIDEO":
        return False
    output_us = int(block[boundary])
    providers = [
        video
        for video in original_video
        if (
            int(video["startUs"]) <= output_us < int(video["endUs"])
            if boundary == "startUs"
            else int(video["startUs"]) < output_us <= int(video["endUs"])
        )
    ]
    return len(providers) == 1 and _video_boundary_uses_clip(
        clip_ranges, providers[0], boundary, clip
    )


def _add_video_block(project: dict[str, Any], payload: dict[str, Any]) -> None:
    project["videoBlocks"].append(
        {
            "id": payload.get("id") or opaque_id("vblock"),
            "startUs": int(payload["startUs"]),
            "endUs": int(payload["endUs"]),
            "logicalSourceId": payload["logicalSourceId"],
            "pinnedClipId": payload.get("pinnedClipId"),
        }
    )


def _split_video_block(project: dict[str, Any], payload: dict[str, Any]) -> None:
    block = _find(project["videoBlocks"], payload["blockId"], "video block")
    at = int(payload["atUs"])
    if not block["startUs"] < at < block["endUs"]:
        raise DomainError("VALIDATION_FAILED", "Split point must be inside video block")
    original_end = block["endUs"]
    block["endUs"] = at
    project["videoBlocks"].append(
        {
            **copy.deepcopy(block),
            "id": payload.get("newBlockId") or opaque_id("vblock"),
            "startUs": at,
            "endUs": original_end,
        }
    )


def _move_video_boundary(project: dict[str, Any], payload: dict[str, Any]) -> None:
    left = _find(project["videoBlocks"], payload["leftBlockId"], "video block")
    right = _find(project["videoBlocks"], payload["rightBlockId"], "video block")
    at = int(payload["atUs"])
    if at <= int(left["startUs"]) or at >= int(right["endUs"]):
        raise DomainError("VALIDATION_FAILED", "Boundary must remain inside adjacent blocks")
    left["endUs"] = at
    right["startUs"] = at


def _delete_video_block(project: dict[str, Any], payload: dict[str, Any]) -> None:
    project["videoBlocks"] = [item for item in project["videoBlocks"] if item["id"] != payload["blockId"]]


def _assign_video_source(project: dict[str, Any], payload: dict[str, Any]) -> None:
    _find(project["logicalSources"], payload["logicalSourceId"], "logical source")
    block = _find(project["videoBlocks"], payload["blockId"], "video block")
    block["logicalSourceId"] = payload["logicalSourceId"]
    block["pinnedClipId"] = None


def _pin_video_clip(project: dict[str, Any], payload: dict[str, Any]) -> None:
    block = _find(project["videoBlocks"], payload["blockId"], "video block")
    clip_id = payload.get("clipId")
    if clip_id is not None:
        clip = _find(project["clips"], clip_id, "project clip")
        if clip["logicalSourceId"] != block["logicalSourceId"]:
            raise DomainError("VALIDATION_FAILED", "Pinned clip belongs to a different logical source")
    block["pinnedClipId"] = clip_id


def _cut_to_source(project: dict[str, Any], payload: dict[str, Any]) -> None:
    block = _find(project["videoBlocks"], payload["blockId"], "video block")
    at = int(payload["atUs"])
    new_block_id = payload.get("newBlockId") or opaque_id("vblock")
    _split_video_block(project, {"blockId": block["id"], "atUs": at, "newBlockId": new_block_id})
    new_block = _find(project["videoBlocks"], new_block_id, "video block")
    new_block["logicalSourceId"] = payload["logicalSourceId"]
    new_block["pinnedClipId"] = payload.get("pinnedClipId")


def _add_audio_block(project: dict[str, Any], payload: dict[str, Any]) -> None:
    rate_ppm = int(payload.get("ratePpm", 0))
    if rate_ppm and not payload.get("confirmDrift"):
        raise DomainError("VALIDATION_FAILED", "Non-zero audio ratePpm requires confirmDrift")
    project["audioBlocks"].append(
        {
            "id": payload.get("id") or opaque_id("ablock"),
            "startUs": int(payload["startUs"]),
            "endUs": int(payload["endUs"]),
            "mode": payload.get("mode", "SILENCE"),
            "logicalSourceId": payload.get("logicalSourceId"),
            "clipId": payload.get("clipId"),
            "offsetUs": int(payload.get("offsetUs", 0)),
            "ratePpm": rate_ppm,
        }
    )


def _split_audio_block(project: dict[str, Any], payload: dict[str, Any]) -> None:
    block = _find(project["audioBlocks"], payload["blockId"], "audio block")
    at = int(payload["atUs"])
    if not block["startUs"] < at < block["endUs"]:
        raise DomainError("VALIDATION_FAILED", "Split point must be inside audio block")
    end = block["endUs"]
    block["endUs"] = at
    project["audioBlocks"].append({**copy.deepcopy(block), "id": opaque_id("ablock"), "startUs": at, "endUs": end})


def _move_audio_boundary(project: dict[str, Any], payload: dict[str, Any]) -> None:
    left = _find(project["audioBlocks"], payload["leftBlockId"], "audio block")
    right = _find(project["audioBlocks"], payload["rightBlockId"], "audio block")
    at = int(payload["atUs"])
    if at <= left["startUs"] or at >= right["endUs"]:
        raise DomainError("VALIDATION_FAILED", "Audio boundary must remain inside adjacent blocks")
    left["endUs"] = at
    right["startUs"] = at


def _delete_audio_block(project: dict[str, Any], payload: dict[str, Any]) -> None:
    project["audioBlocks"] = [item for item in project["audioBlocks"] if item["id"] != payload["blockId"]]


def _set_audio_mode(project: dict[str, Any], payload: dict[str, Any]) -> None:
    block = _find(project["audioBlocks"], payload["blockId"], "audio block")
    mode = payload["mode"]
    if mode not in AUDIO_MODES:
        raise DomainError("VALIDATION_FAILED", "Unknown audio mode")
    block.update(
        mode=mode,
        logicalSourceId=payload.get("logicalSourceId"),
        clipId=payload.get("clipId"),
        offsetUs=int(payload.get("offsetUs", 0)),
        ratePpm=int(payload.get("ratePpm", 0)),
    )
    if block["ratePpm"] and not payload.get("confirmDrift"):
        raise DomainError("VALIDATION_FAILED", "Non-zero audio ratePpm requires confirmDrift")


def _set_anchor_mode(project: dict[str, Any], payload: dict[str, Any]) -> None:
    mode = payload["anchorMode"]
    if mode not in ANCHOR_MODES:
        raise DomainError("VALIDATION_FAILED", "Unknown anchor mode")
    project["anchorMode"] = mode


def _reconcile_boundary(project: dict[str, Any], payload: dict[str, Any]) -> None:
    operation = payload["operation"]
    if operation in {"CLOSE_GAP", "TRIM_OVERLAP"}:
        _move_video_boundary(project, payload)
        return
    if operation == "NORMALIZE_RANGE":
        start_us = int(payload["startUs"])
        end_us = int(payload["endUs"])
        blocks = sorted(
            [item for item in project["videoBlocks"] if item["endUs"] > start_us and item["startUs"] < end_us],
            key=lambda item: item["startUs"],
        )
        for left, right in zip(blocks, blocks[1:]):
            right["startUs"] = left["endUs"]
        return
    raise DomainError("VALIDATION_FAILED", "Unknown reconciliation operation")


def _accept_suggestion(
    project: dict[str, Any], payload: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> None:
    _set_sync(
        project,
        {
            "clipId": payload["clipId"],
            "sync": payload["sync"],
            "confirmDrift": bool(payload.get("confirmDrift")),
        },
        assets,
    )


def _clip_ranges(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    *,
    include_provisional: bool = False,
) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for clip in project.get("clips", []):
        asset = assets.get(clip["assetId"])
        if not asset or asset.get("missing"):
            continue
        state = clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED")
        if state == "UNRESOLVED" or (not include_provisional and state != "ACCEPTED"):
            continue
        duration_us = _asset_duration_us(asset)
        if duration_us <= 0:
            continue
        transform = _clip_alignment(clip)
        ranges.append(
            {
                "clipId": clip["id"],
                "assetId": clip["assetId"],
                "logicalSourceId": clip["logicalSourceId"],
                "startUs": transform.source_to_output(0),
                "endUs": transform.source_to_output(duration_us),
                "transform": transform,
            }
        )
    return ranges


def _compile_source_block(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    block: dict[str, Any],
    issues: list[dict[str, Any]],
    require_audio: bool,
) -> list[dict[str, Any]]:
    ranges = [
        item
        for item in _clip_ranges(project, assets)
        if item["logicalSourceId"] == block["logicalSourceId"]
        and (not block.get("pinnedClipId") or item["clipId"] == block["pinnedClipId"])
    ]
    boundaries = {int(block["startUs"]), int(block["endUs"])}
    for item in ranges:
        if item["endUs"] > block["startUs"] and item["startUs"] < block["endUs"]:
            boundaries.add(max(int(block["startUs"]), int(item["startUs"])))
            boundaries.add(min(int(block["endUs"]), int(item["endUs"])))
    points = sorted(boundaries)
    slices: list[dict[str, Any]] = []
    for start_us, end_us in zip(points, points[1:]):
        if end_us <= start_us:
            continue
        candidates = [item for item in ranges if item["startUs"] <= start_us and item["endUs"] >= end_us]
        if require_audio:
            candidates = [item for item in candidates if _asset_has_audio(assets.get(item["assetId"], {}))]
        if not candidates:
            issues.append(
                _issue(
                    "SOURCE_UNAVAILABLE" if not require_audio else "AUDIO_UNAVAILABLE",
                    start_us,
                    end_us,
                    "Selected source has no usable clip for this interval",
                    blockId=block["id"],
                )
            )
            continue
        if len(candidates) > 1:
            issues.append(
                _issue(
                    "AMBIGUOUS",
                    start_us,
                    end_us,
                    "Multiple clips can cover this interval; pin one clip",
                    blockId=block["id"],
                    clipIds=[item["clipId"] for item in candidates],
                )
            )
            continue
        selected = candidates[0]
        transform: SyncTransform = selected["transform"]
        slices.append(
            {
                "id": _compiled_slice_id(
                    "aslice" if require_audio else "vslice",
                    block["id"],
                    start_us,
                    end_us,
                    selected["assetId"],
                    selected["clipId"],
                ),
                "blockId": block["id"],
                "startUs": start_us,
                "endUs": end_us,
                "logicalSourceId": selected["logicalSourceId"],
                "clipId": selected["clipId"],
                "assetId": selected["assetId"],
                "streamId": _primary_stream_id(
                    assets.get(selected["assetId"], {}), "audio" if require_audio else "video"
                ),
                "sourceStartUs": transform.output_to_source(start_us),
                "sourceEndUs": transform.output_to_source(end_us),
                "sync": transform.to_legacy_sync_dict(),
                "alignment": transform.to_dict(),
            }
        )
    return slices


def _compile_audio_block(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    block: dict[str, Any],
    video_slices: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mode = block["mode"]
    if mode == "SILENCE":
        return [
            {
                "id": _compiled_slice_id("aslice", block["id"], block["startUs"], block["endUs"], "silence"),
                "blockId": block["id"],
                "startUs": block["startUs"],
                "endUs": block["endUs"],
                "mode": "SILENCE",
                "synthetic": True,
                "transforms": ["generated silence"],
            }
        ]
    if mode == "FOLLOW_VIDEO":
        slices: list[dict[str, Any]] = []
        relevant = [
            item
            for item in video_slices
            if item["endUs"] > block["startUs"] and item["startUs"] < block["endUs"]
        ]
        for video in relevant:
            start_us = max(int(block["startUs"]), int(video["startUs"]))
            end_us = min(int(block["endUs"]), int(video["endUs"]))
            asset = assets.get(video["assetId"], {})
            if not _asset_has_audio(asset):
                issues.append(
                    _issue(
                        "AUDIO_UNAVAILABLE",
                        start_us,
                        end_us,
                        "FOLLOW_VIDEO selected a video clip without usable audio",
                        blockId=block["id"],
                        assetId=video["assetId"],
                    )
                )
                continue
            slices.append(
                {
                    **copy.deepcopy(video),
                    "id": _compiled_slice_id(
                        "aslice", block["id"], start_us, end_us, video["assetId"], video["clipId"]
                    ),
                    "blockId": block["id"],
                    "startUs": start_us,
                    "endUs": end_us,
                    "streamId": _primary_stream_id(asset, "audio"),
                    "mode": mode,
                }
            )
        return _apply_audio_timing(slices, block, assets, issues)
    selected_clip = (
        _find(project["clips"], block.get("clipId"), "project clip")
        if mode == "FIXED_CLIP"
        else None
    )
    synthetic_block = {
        "id": block["id"],
        "startUs": block["startUs"],
        "endUs": block["endUs"],
        "logicalSourceId": (
            selected_clip["logicalSourceId"]
            if selected_clip
            else block.get("logicalSourceId")
        ),
        "pinnedClipId": block.get("clipId") if mode == "FIXED_CLIP" else None,
    }
    slices = _compile_source_block(project, assets, synthetic_block, issues, require_audio=True)
    for item in slices:
        item["mode"] = mode
    return _apply_audio_timing(slices, block, assets, issues)


def _apply_audio_timing(
    slices: list[dict[str, Any]],
    block: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compile independent audio timing into exact source ranges.

    The renderer and manifest consume these ranges directly. Keeping offset/rate
    application here prevents editorial decisions, preflight, and output media
    from disagreeing about which source samples are selected.
    """

    offset_us = int(block.get("offsetUs", 0))
    rate_ppm = int(block.get("ratePpm", 0))
    rate_denominator = 1_000_000 + rate_ppm
    anchor_output_us = int(block["startUs"])
    for item in slices:
        sync = SyncTransform.from_dict(item.get("sync"))
        anchor_source_us = sync.output_to_source(anchor_output_us)

        def source_at(output_us: int) -> int:
            base_delta = sync.output_to_source(output_us) - anchor_source_us
            return anchor_source_us + _round_ratio(base_delta * 1_000_000, rate_denominator) + offset_us

        item["sourceStartUs"] = source_at(int(item["startUs"]))
        item["sourceEndUs"] = source_at(int(item["endUs"]))
        item["offsetUs"] = offset_us
        item["ratePpm"] = rate_ppm
        transforms = list(item.get("transforms", []))
        if offset_us:
            transforms.append(f"audio source offset {offset_us} us")
        if rate_ppm:
            transforms.append(f"audio rate correction {rate_ppm} ppm")
        item["transforms"] = transforms
        asset = assets.get(item["assetId"], {})
        duration_us = _asset_duration_us(asset)
        if (
            item["sourceStartUs"] < 0
            or item["sourceEndUs"] <= item["sourceStartUs"]
            or (duration_us > 0 and item["sourceEndUs"] > duration_us)
        ):
            issues.append(
                _issue(
                    "AUDIO_UNAVAILABLE",
                    int(item["startUs"]),
                    int(item["endUs"]),
                    "Independent audio timing exceeds the selected source range",
                    blockId=block["id"],
                    assetId=item["assetId"],
                )
            )
    return slices


def _asset_has_audio(asset: dict[str, Any]) -> bool:
    if asset.get("audio_codec"):
        return True
    return any(stream.get("codecType") == "audio" for stream in asset.get("streams", []))


def _primary_stream_id(asset: dict[str, Any], codec_type: str) -> str | None:
    return next(
        (
            str(stream["id"])
            for stream in asset.get("streams", [])
            if stream.get("codecType") == codec_type and stream.get("id")
        ),
        None,
    )


def _interval_issues(
    blocks: list[dict[str, Any]], duration_us: int, prefix: str, issues: list[dict[str, Any]]
) -> None:
    cursor = 0
    for block in blocks:
        start_us, end_us = int(block["startUs"]), int(block["endUs"])
        if start_us > cursor:
            issues.append(_issue(f"{prefix}_GAP", cursor, start_us, f"{prefix.title()} gap requires explicit media"))
        if start_us < cursor:
            issues.append(
                _issue(f"{prefix}_OVERLAP", start_us, min(cursor, end_us), f"{prefix.title()} blocks overlap")
            )
        cursor = max(cursor, end_us)
    if cursor < duration_us:
        issues.append(_issue(f"{prefix}_GAP", cursor, duration_us, f"{prefix.title()} gap requires explicit media"))


def _issue(code: str, start_us: int, end_us: int, message: str, **refs: Any) -> dict[str, Any]:
    return {
        "id": f"issue_{digest_json([code, start_us, end_us, refs])[:20]}",
        "code": code,
        "severity": BLOCKING,
        "startUs": start_us,
        "endUs": end_us,
        "message": message,
        "refs": refs,
    }


def _compiled_slice_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{digest_json([str(part) for part in parts])[:24]}"


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for issue in issues:
        unique[issue["id"]] = issue
    return sorted(unique.values(), key=lambda item: (item["startUs"], item["endUs"], item["code"]))


def _validate_interval(value: dict[str, Any]) -> None:
    try:
        start_us, end_us = int(value["startUs"]), int(value["endUs"])
    except (KeyError, TypeError, ValueError) as error:
        raise DomainError("VALIDATION_FAILED", "Interval requires integer startUs and endUs") from error
    if start_us < 0 or end_us <= start_us:
        raise DomainError("VALIDATION_FAILED", "Intervals must satisfy 0 <= startUs < endUs")


def _unique_ids(items: list[dict[str, Any]], label: str) -> None:
    ids = [item.get("id") for item in items]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise DomainError("VALIDATION_FAILED", f"Every {label} needs a unique opaque ID")


def _find(items: list[dict[str, Any]], item_id: str, label: str) -> dict[str, Any]:
    for item in items:
        if item.get("id") == item_id:
            return item
    raise DomainError("NOT_FOUND", f"Unknown {label}: {item_id}")
