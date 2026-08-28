from __future__ import annotations

import copy
import hashlib
import json
import uuid
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any


MAX_RATE_PPM = 2_000
AUDIO_MODES = {"FOLLOW_VIDEO", "FIXED_SOURCE", "FIXED_CLIP", "SILENCE"}
ANCHOR_MODES = {"PROGRAM_TIME", "SOURCE_TIME"}
ALIGNMENT_STATES = {"PROVISIONAL", "ACCEPTED", "REVIEW_REQUIRED", "UNRESOLVED"}
PROGRAM_ELIGIBILITY_STATES = {"ELIGIBLE", "HELD_FOR_REVIEW", "EXCLUDED"}
SOURCE_IDENTITY_STATES = {"PROVISIONAL", "USER_CONFIRMED"}
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
    "ConfirmSourceIdentities": {"sourceIds"},
    "SetReferenceSource": {"sourceId"},
    "SetSyncTransform": {"clipId", "sync", "confirmDrift"},
    "SetClipAlignment": {"clipId", "alignment", "confirmDrift"},
    "AlignMarkedMoments": {
        "referenceClipId", "referenceSourceUs", "targetClipId", "targetSourceUs"
    },
    "AcceptAlignmentProposalSet": {
        "proposalSetId", "digest", "mode", "scope", "previewId", "previewDigest",
        "confirmTimestampUncertainty", "confirmDrift", "alignments"
    },
    "AcceptAlignmentProposal": {
        "proposalSetId", "proposalId", "digest", "confirmLowConfidence", "confirmDrift",
        "alignments"
    },
    "RejectAlignmentProposal": {"proposalSetId", "proposalId", "digest"},
    "RejectAlignmentProposalSet": {"proposalSetId", "digest"},
    "SetClipProgramEligibility": {"clipIds", "programEligibility", "rationale"},
    "SetRangeProgramEligibility": {
        "startAlignedUs", "endAlignedUs", "sourceIds", "currentEligibilityFilter",
        "programEligibility", "rationale",
    },
    "SetTimelineSections": {"sections"},
    "GenerateProgramDraft": {
        "alignmentDigest", "selectionDigest", "gapMode", "sectionProposalDigest", "replaceExisting",
    },
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
        # Candidate evidence creates manageable provisional tracks, not accepted
        # camera identity. Unknown candidates stay isolated. A later named
        # command is the only way to confirm these tracks for program use.
        candidate_groups: dict[str, dict[str, Any]] = {}
        for index, asset in enumerate(chosen):
            candidate_key = str(asset.get("sourceCandidateId") or f"asset:{asset['id']}")
            group = candidate_groups.setdefault(
                candidate_key,
                {
                    "label": asset.get("camera") or f"Source candidate {index + 1}",
                    "assetIds": [],
                    "candidateKey": candidate_key,
                },
            )
            group["assetIds"].append(asset["id"])
        groups = list(candidate_groups.values())
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
                "identityState": "USER_CONFIRMED" if source_groups is not None else "PROVISIONAL",
                "candidateEvidence": {
                    "candidateKeys": candidate_keys,
                    "assetCount": len(group_assets),
                },
            }
        )
        for asset in group_assets:
            clip = {
                "id": opaque_id("clip"),
                "assetId": asset["id"],
                "logicalSourceId": source_id,
                "programEligibility": (
                    "ELIGIBLE" if initialize_legacy_program else "HELD_FOR_REVIEW"
                ),
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
                        "programEligibility": "HELD_FOR_REVIEW",
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
        "syntheticSlates": [],
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
                "programEligibility": _program_eligibility(clip),
            }
            for clip in sorted(project.get("clips", []), key=lambda item: item["id"])
        ]
    )


def aligned_extent(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    *,
    include_provisional: bool = True,
    include_unavailable: bool = False,
) -> dict[str, int]:
    ranges = _clip_ranges(
        project,
        assets,
        include_provisional=include_provisional,
        include_unavailable=include_unavailable,
    )
    if not ranges:
        return {"startAlignedUs": 0, "endAlignedUs": 0, "durationUs": 0}
    start_us = min(int(item["startUs"]) for item in ranges)
    end_us = max(int(item["endUs"]) for item in ranges)
    return {"startAlignedUs": start_us, "endAlignedUs": end_us, "durationUs": end_us - start_us}


def _alignment_state(clip: dict[str, Any]) -> str:
    return str(clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED"))


def _program_eligibility(clip: dict[str, Any]) -> str:
    return str(
        clip.get(
            "programEligibility",
            "ELIGIBLE" if _alignment_state(clip) == "ACCEPTED" else "HELD_FOR_REVIEW",
        )
    )


def _union_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((int(start), int(end)) for start, end in intervals if int(end) > int(start))
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _interval_duration(intervals: Iterable[tuple[int, int]]) -> int:
    return sum(end - start for start, end in _union_intervals(intervals))


def _difference_duration(
    required: Iterable[tuple[int, int]], covered: Iterable[tuple[int, int]]
) -> int:
    required_intervals = _union_intervals(required)
    covered_intervals = _union_intervals(covered)
    missing = 0
    covered_index = 0
    for start, end in required_intervals:
        cursor = start
        while covered_index < len(covered_intervals) and covered_intervals[covered_index][1] <= start:
            covered_index += 1
        scan_index = covered_index
        while scan_index < len(covered_intervals) and covered_intervals[scan_index][0] < end:
            covered_start, covered_end = covered_intervals[scan_index]
            if covered_start > cursor:
                missing += min(end, covered_start) - cursor
            cursor = max(cursor, covered_end)
            if cursor >= end:
                break
            scan_index += 1
        if cursor < end:
            missing += end - cursor
    return missing


def alignment_summary(
    project: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    all_ranges = _clip_ranges(
        project, assets, include_provisional=True, include_unavailable=True
    )
    clips_by_id = {str(item["id"]): item for item in project.get("clips", [])}
    accepted_ranges = [
        item
        for item in all_ranges
        if _alignment_state(clips_by_id[str(item["clipId"])]) == "ACCEPTED"
        and _program_eligibility(clips_by_id[str(item["clipId"])]) == "ELIGIBLE"
        and not assets[str(item["assetId"])].get("missing")
    ]
    extent = aligned_extent(
        project,
        assets,
        include_provisional=True,
        include_unavailable=True,
    )
    evidence_intervals = [(int(item["startUs"]), int(item["endUs"])) for item in all_ranges]
    accepted_intervals = [(int(item["startUs"]), int(item["endUs"])) for item in accepted_ranges]
    evidence_coverage_us = _interval_duration(evidence_intervals)
    accepted_coverage_us = _interval_duration(accepted_intervals)
    unresolved_sole_coverage_us = _difference_duration(evidence_intervals, accepted_intervals)
    counts = {state: 0 for state in sorted(ALIGNMENT_STATES)}
    audio_confirmed = 0
    timestamp_only = 0
    eligibility_counts = {state: 0 for state in sorted(PROGRAM_ELIGIBILITY_STATES)}
    for clip in project.get("clips", []):
        state = _alignment_state(clip)
        counts[state] = counts.get(state, 0) + 1
        eligibility = _program_eligibility(clip)
        eligibility_counts[eligibility] = eligibility_counts.get(eligibility, 0) + 1
        evidence = set(clip.get("alignmentEvidence", []))
        if "audio-correlation" in evidence:
            audio_confirmed += 1
        elif "timestamp" in evidence or "timestamp-prior" in evidence:
            timestamp_only += 1
    evidence_union = _union_intervals(evidence_intervals)
    sections = project.get("timelineSections", [])
    required_sections = (
        [
            (int(item["startAlignedUs"]), int(item["endAlignedUs"]), str(item["mode"]))
            for item in sections
        ]
        if sections
        else [(start, end, "KEEP") for start, end in evidence_union]
    )
    boundaries = {
        value
        for start, end, _mode in required_sections
        for value in (start, end)
    }
    boundaries.update(
        value for item in all_ranges for value in (int(item["startUs"]), int(item["endUs"]))
    )
    ordered_boundaries = sorted(boundaries)
    coverage_intervals: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    ordered_sections = sorted(required_sections)
    section_index = 0
    starts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    ends: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in all_ranges:
        starts[int(item["startUs"])].append(item)
        ends[int(item["endUs"])].append(item)
    active: dict[str, Counter[str]] = {
        name: Counter()
        for name in (
            "accepted_eligible",
            "accepted_held",
            "provisional",
            "conflicting",
            "unavailable",
        )
    }

    def range_category(item: dict[str, Any]) -> str:
        clip = clips_by_id[str(item["clipId"])]
        asset = assets.get(str(item["assetId"]))
        state = _alignment_state(clip)
        eligibility = _program_eligibility(clip)
        if not asset or asset.get("missing"):
            return "unavailable"
        if state == "ACCEPTED" and eligibility == "ELIGIBLE":
            return "accepted_eligible"
        if state == "ACCEPTED":
            return "accepted_held"
        if state == "REVIEW_REQUIRED":
            return "conflicting"
        return "provisional"

    for start, end in zip(ordered_boundaries, ordered_boundaries[1:]):
        for item in ends.get(start, []):
            category = range_category(item)
            clip_id = str(item["clipId"])
            active[category][clip_id] -= 1
            if active[category][clip_id] <= 0:
                del active[category][clip_id]
        for item in starts.get(start, []):
            active[range_category(item)][str(item["clipId"])] += 1
        while (
            section_index < len(ordered_sections)
            and ordered_sections[section_index][1] <= start
        ):
            section_index += 1
        section = (
            ordered_sections[section_index]
            if section_index < len(ordered_sections)
            and ordered_sections[section_index][0] <= start
            and end <= ordered_sections[section_index][1]
            else None
        )
        has_evidence = any(active[category] for category in active)
        if section is None and not has_evidence:
            continue
        mode = section[2] if section is not None else "UNASSIGNED"
        accepted_eligible = sorted(active["accepted_eligible"])
        accepted_held = sorted(active["accepted_held"])
        provisional = sorted(active["provisional"])
        conflicting = sorted(active["conflicting"])
        unavailable = sorted(active["unavailable"])
        blocker_codes: list[str] = []
        warning_codes: list[str] = []
        if mode == "UNASSIGNED":
            readiness = "BLOCKED"
            blocker_codes.append("TIMELINE_SECTION_REQUIRED")
        elif mode == "EXCLUDE":
            readiness = "EXCLUDED"
        elif mode == "SLATE":
            readiness = "SYNTHETIC"
        elif accepted_eligible:
            readiness = "READY_WITH_WARNINGS" if (provisional or conflicting or unavailable) else "READY"
            if provisional:
                warning_codes.append("REDUNDANT_TIMESTAMP_ONLY_CLIP")
            if conflicting:
                warning_codes.append("REDUNDANT_CONFLICTING_CLIP")
            if unavailable:
                warning_codes.append("REDUNDANT_UNAVAILABLE_CLIP")
        else:
            readiness = "BLOCKED"
            if conflicting:
                blocker_codes.append("SOLE_COVERAGE_CONFLICTING")
            elif unavailable:
                blocker_codes.append("SOLE_COVERAGE_UNAVAILABLE")
            elif provisional or accepted_held:
                blocker_codes.append("SOLE_COVERAGE_TIMING_UNRESOLVED")
            else:
                blocker_codes.append("NO_ACCEPTED_ELIGIBLE_VIDEO")
        interval = {
            "startAlignedUs": start,
            "endAlignedUs": end,
            "sectionMode": mode,
            "acceptedEligibleVideoClipIds": accepted_eligible,
            "acceptedHeldVideoClipIds": accepted_held,
            "provisionalVideoClipIds": provisional,
            "conflictingVideoClipIds": conflicting,
            "unavailableVideoClipIds": unavailable,
            "ambiguityState": "NONE",
            "readiness": readiness,
            "blockerCodes": blocker_codes,
            "warningCodes": warning_codes,
        }
        coverage_intervals.append(interval)
        for code in blocker_codes:
            blockers.append({
                "code": code, "startAlignedUs": start, "endAlignedUs": end,
                "clipIds": sorted(set(accepted_held + provisional + conflicting + unavailable)),
                "blocking": True,
                "remediationActions": ["ACCEPT_ALIGNMENT", "EDIT_ALIGNMENT", "EXCLUDE_RANGE", "ADD_SLATE"],
            })
        for code in warning_codes:
            warnings.append({
                "code": code, "startAlignedUs": start, "endAlignedUs": end,
                "clipIds": sorted(set(provisional + conflicting + unavailable)), "blocking": False,
            })
    proposed_output_duration_us = (
        sum(
            int(section["endAlignedUs"]) - int(section["startAlignedUs"])
            for section in sections
            if section.get("mode") in {"KEEP", "SLATE"}
        )
        if sections
        else int(extent["durationUs"])
    )
    unresolved_duration_clip_ids = sorted(
        str(clip["id"])
        for clip in project.get("clips", [])
        if (
            not assets.get(str(clip["assetId"]))
            or _asset_duration_us(assets[str(clip["assetId"])]) <= 0
        )
        and _program_eligibility(clip) != "EXCLUDED"
    )
    if unresolved_duration_clip_ids:
        blockers.append(
            {
                "code": "DURATION_UNRESOLVED",
                "clipIds": unresolved_duration_clip_ids,
                "blocking": True,
                "remediationActions": ["RESCAN", "EXCLUDE_CLIP"],
            }
        )
    ready = bool(accepted_ranges) and not blockers
    unresolved_sole_coverage_us = _interval_duration(
        (int(item["startAlignedUs"]), int(item["endAlignedUs"]))
        for item in blockers
        if "startAlignedUs" in item and "endAlignedUs" in item
    )
    return {
        "projectId": project["id"],
        "revision": int(project["revision"]),
        "alignmentDigest": alignment_digest(project),
        "evidenceSpan": extent,
        "proposedOutputDurationUs": proposed_output_duration_us,
        "confidenceCounts": {
            "accepted": counts.get("ACCEPTED", 0),
            "provisional": counts.get("PROVISIONAL", 0),
            "reviewRequired": counts.get("REVIEW_REQUIRED", 0),
            "unresolved": counts.get("UNRESOLVED", 0),
            "audioConfirmed": audio_confirmed,
            "timestampOnly": timestamp_only,
        },
        "eligibilityCounts": {
            "eligible": eligibility_counts.get("ELIGIBLE", 0),
            "heldForReview": eligibility_counts.get("HELD_FOR_REVIEW", 0),
            "excluded": eligibility_counts.get("EXCLUDED", 0),
        },
        "coverage": {
            "evidenceCoverageUs": evidence_coverage_us,
            "acceptedCoverageUs": accepted_coverage_us,
            "unresolvedSoleCoverageUs": unresolved_sole_coverage_us,
            "acceptedPercent": (
                round(accepted_coverage_us * 100 / evidence_coverage_us, 3)
                if evidence_coverage_us
                else 0.0
            ),
            "sourceCount": len({item["logicalSourceId"] for item in all_ranges}),
        },
        "coverageIntervals": coverage_intervals,
        "blockers": blockers,
        "warnings": warnings,
        "conflicts": blockers + warnings,
        "unplacedClipIds": [
            str(clip["id"])
            for clip in project.get("clips", [])
            if _alignment_state(clip) == "UNRESOLVED"
        ],
        "readyForProgramDraft": ready,
    }


def project_preparation(
    project: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Return backend-owned workflow readiness without mutating project state."""

    alignment = alignment_summary(project, assets)
    active_sources = [
        source for source in project.get("logicalSources", []) if not source.get("archived")
    ]
    provisional_source_ids = [
        str(source["id"])
        for source in active_sources
        if source.get("identityState", "USER_CONFIRMED") != "USER_CONFIRMED"
    ]
    has_program = bool(project.get("videoBlocks"))
    compiled = compile_program(project, assets) if has_program else None
    program_duration_us = int(compiled["durationUs"]) if compiled else 0
    aligned_end_us = int(alignment["evidenceSpan"]["endAlignedUs"])
    aligned_start_us = int(alignment["evidenceSpan"]["startAlignedUs"])
    evidence_duration_us = int(alignment["evidenceSpan"]["durationUs"])
    program_draft = project.get("programDraft") or {}
    has_explicit_composition = bool(
        project.get("timelineSections")
        and program_draft.get("strategy") == "coverage-optimizer-v1"
        and program_draft.get("alignmentDigest")
        and program_draft.get("selectionDigest")
    )
    legacy_truncation = bool(
        has_program
        and not has_explicit_composition
        and evidence_duration_us > 0
        and aligned_end_us - aligned_start_us > program_duration_us + 1_000_000
    )
    source_ready = not provisional_source_ids
    alignment_ready = bool(alignment["readyForProgramDraft"])
    composition_resolves_alignment = False
    if source_ready and not alignment_ready:
        try:
            section_proposal = timeline_section_proposal(project, assets, "EXCLUDE")
            composed = copy.deepcopy(project)
            _set_timeline_sections(composed, {"sections": section_proposal["sections"]})
            composition_resolves_alignment = bool(
                alignment_summary(composed, assets)["readyForProgramDraft"]
            )
        except DomainError:
            composition_resolves_alignment = False
    if project.get("review") is not None and not legacy_truncation:
        phase = "REVIEWED"
    elif has_program:
        phase = "PROGRAM_DRAFT"
    elif not source_ready:
        phase = "SOURCE_REVIEW"
    elif not alignment_ready:
        phase = "ALIGNMENT_REVIEW"
    else:
        phase = "COMPOSITION_READY"
    blockers: list[dict[str, Any]] = []
    if provisional_source_ids:
        blockers.append(
            {
                "code": "SOURCE_IDENTITY_UNCONFIRMED",
                "count": len(provisional_source_ids),
                "sourceIds": provisional_source_ids,
            }
        )
    if not alignment_ready:
        blockers.extend(alignment.get("blockers", []))
    if legacy_truncation:
        blockers.append(
            {
                "code": "PROGRAM_TRUNCATES_ALIGNED_MEDIA",
                "programDurationUs": program_duration_us,
                "evidenceDurationUs": evidence_duration_us,
            }
        )
    return {
        "projectId": project["id"],
        "revision": int(project["revision"]),
        "phase": phase,
        "sourceIdentity": {
            "ready": source_ready,
            "confirmedCount": len(active_sources) - len(provisional_source_ids),
            "provisionalCount": len(provisional_source_ids),
            "provisionalSourceIds": provisional_source_ids,
        },
        "alignment": alignment,
        "hasProgram": has_program,
        "programDurationUs": program_duration_us,
        "legacyProgramTruncation": legacy_truncation,
        "canAnalyzeAlignment": bool(project.get("clips")),
        "canGenerateProgramDraft": source_ready
        and (alignment_ready or composition_resolves_alignment),
        "compositionResolvesAlignment": composition_resolves_alignment,
        "canEnterCut": has_program and not legacy_truncation,
        "canEnterReview": bool(compiled and compiled.get("valid") and not legacy_truncation),
        "blockers": blockers,
    }


def timeline_window(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    start_aligned_us: int,
    end_aligned_us: int,
    resolution_us: int,
    lane_ids: set[str] | None = None,
    *,
    max_items: int = 2_000,
) -> dict[str, Any]:
    start_aligned_us = int(start_aligned_us)
    end_aligned_us = int(end_aligned_us)
    resolution_us = int(resolution_us)
    max_items = max(1, min(int(max_items), 2_000))
    if end_aligned_us <= start_aligned_us:
        raise DomainError("VALIDATION_FAILED", "Timeline window must have positive duration")
    if resolution_us <= 0:
        raise DomainError("VALIDATION_FAILED", "resolutionUs must be positive")
    clip_by_id = {str(item["id"]): item for item in project.get("clips", [])}
    unplaced_candidates: list[dict[str, Any]] = []
    for clip in project.get("clips", []):
        if _alignment_state(clip) != "UNRESOLVED":
            continue
        if lane_ids and str(clip["logicalSourceId"]) not in lane_ids:
            continue
        asset = assets.get(str(clip["assetId"]), {})
        warnings = ["TIMING_UNRESOLVED"]
        if asset.get("warning"):
            warnings.append(str(asset["warning"]))
        if asset.get("missing"):
            warnings.append("SOURCE_UNAVAILABLE")
        unplaced_candidates.append(
            {
                "type": "UNPLACED",
                "clipId": str(clip["id"]),
                "assetId": str(clip["assetId"]),
                "logicalSourceId": str(clip["logicalSourceId"]),
                "alignmentState": "UNRESOLVED",
                "relativePath": str(asset.get("relative_path", "")),
                "warnings": warnings,
            }
        )
    unplaced_candidates.sort(key=lambda item: (item["logicalSourceId"], item["clipId"]))
    unplaced_items = unplaced_candidates[:max_items]
    visible_budget = max(0, max_items - len(unplaced_items))
    items: list[dict[str, Any]] = []
    total_before = 0
    for item in _clip_ranges(project, assets, include_provisional=True):
        if lane_ids and str(item["logicalSourceId"]) not in lane_ids:
            continue
        if int(item["endUs"]) <= start_aligned_us:
            total_before += 1
            continue
        if int(item["startUs"]) >= end_aligned_us:
            continue
        clip = clip_by_id[str(item["clipId"])]
        asset = assets.get(item["assetId"], {})
        warnings = []
        if asset.get("warning"):
            warnings.append(str(asset["warning"]))
        if asset.get("missing"):
            warnings.append("SOURCE_UNAVAILABLE")
        state = _alignment_state(clip)
        if state in {"REVIEW_REQUIRED", "UNRESOLVED"}:
            warnings.append(f"TIMING_{state}")
        items.append(
            {
                "type": "CLIP",
                "clipId": item["clipId"],
                "assetId": item["assetId"],
                "logicalSourceId": item["logicalSourceId"],
                "startAlignedUs": int(item["startUs"]),
                "endAlignedUs": int(item["endUs"]),
                "alignmentState": state,
                "programEligibility": _program_eligibility(clip),
                "confidence": float(clip.get("alignmentConfidence", 1.0 if "sync" in clip else 0.0)),
                "evidenceKinds": list(clip.get("alignmentEvidence", [])),
                "relativePath": str(asset.get("relative_path", "")),
                "warnings": warnings,
            }
        )
    items.sort(key=lambda item: (item["startAlignedUs"], item["clipId"]))
    total_in_window = len(items)
    if total_in_window <= visible_budget:
        mode = "EXACT"
        response_items = items
        effective_resolution = resolution_us
    else:
        mode = "AGGREGATED"
        span = end_aligned_us - start_aligned_us
        effective_resolution = max(
            resolution_us,
            (span + max(1, visible_budget) - 1) // max(1, visible_budget),
        )
        bucket_count = min(
            visible_budget,
            (span + effective_resolution - 1) // effective_resolution,
        )
        buckets: list[dict[str, Any]] = []
        starts = sorted(items, key=lambda item: (item["startAlignedUs"], item["clipId"]))
        active: dict[str, dict[str, Any]] = {}
        start_index = 0
        for bucket_index in range(bucket_count):
            bucket_start = start_aligned_us + bucket_index * effective_resolution
            bucket_end = min(end_aligned_us, bucket_start + effective_resolution)
            while start_index < len(starts) and starts[start_index]["startAlignedUs"] < bucket_end:
                active[starts[start_index]["clipId"]] = starts[start_index]
                start_index += 1
            active = {
                clip_id: item
                for clip_id, item in active.items()
                if item["endAlignedUs"] > bucket_start
            }
            if not active:
                continue
            state_counts: dict[str, int] = {}
            for item in active.values():
                state = str(item["alignmentState"])
                state_counts[state] = state_counts.get(state, 0) + 1
            buckets.append(
                {
                    "type": "BUCKET",
                    "startAlignedUs": bucket_start,
                    "endAlignedUs": bucket_end,
                    "clipCount": len(active),
                    "sourceCount": len({item["logicalSourceId"] for item in active.values()}),
                    "logicalSourceIds": sorted({item["logicalSourceId"] for item in active.values()}),
                    "stateCounts": state_counts,
                    "warningCount": sum(bool(item["warnings"]) for item in active.values()),
                }
            )
        response_items = buckets[:visible_budget]
    return {
        "projectId": project["id"],
        "revision": int(project["revision"]),
        "startAlignedUs": start_aligned_us,
        "endAlignedUs": end_aligned_us,
        "resolutionUs": effective_resolution,
        "mode": mode,
        "items": response_items,
        "totalBeforeWindow": total_before,
        "totalInWindow": total_in_window,
        "unplacedCount": len(unplaced_candidates),
        "unplacedItems": unplaced_items,
        "alignmentDigest": alignment_digest(project),
    }


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
    for source in project["logicalSources"]:
        identity_state = source.get("identityState", "USER_CONFIRMED")
        if identity_state not in SOURCE_IDENTITY_STATES:
            raise DomainError(
                "VALIDATION_FAILED",
                f"Logical source {source['id']} has an invalid identity state",
            )
    clip_ids = {item["id"] for item in project["clips"]}
    for clip in project["clips"]:
        if clip.get("logicalSourceId") not in source_ids:
            raise DomainError("VALIDATION_FAILED", f"Clip {clip['id']} references an unknown logical source")
        _clip_alignment(clip)
        state = clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED")
        if state not in ALIGNMENT_STATES:
            raise DomainError("VALIDATION_FAILED", f"Clip {clip['id']} has an invalid alignment state")
        eligibility = _program_eligibility(clip)
        if eligibility not in PROGRAM_ELIGIBILITY_STATES:
            raise DomainError("VALIDATION_FAILED", f"Clip {clip['id']} has invalid program eligibility")
        if eligibility == "ELIGIBLE" and state != "ACCEPTED":
            raise DomainError("VALIDATION_FAILED", f"Clip {clip['id']} must be accepted before eligibility")
    for section in project.get("timelineSections", []):
        if section.get("mode") not in TIMELINE_SECTION_MODES:
            raise DomainError("VALIDATION_FAILED", f"Timeline section {section.get('id')} has an invalid mode")
        if int(section.get("endAlignedUs", 0)) <= int(section.get("startAlignedUs", 0)):
            raise DomainError("VALIDATION_FAILED", "Timeline sections must have positive aligned duration")
    for block in project["videoBlocks"]:
        _validate_interval(block)
        synthetic_slate_id = block.get("syntheticSlateId")
        if synthetic_slate_id is not None:
            if block.get("logicalSourceId") is not None:
                raise DomainError("VALIDATION_FAILED", "Synthetic slate blocks may not select a source")
            if synthetic_slate_id not in {
                str(item["id"]) for item in project.get("syntheticSlates", [])
            }:
                raise DomainError("VALIDATION_FAILED", "Video block references an unknown synthetic slate")
        elif block.get("logicalSourceId") not in source_ids:
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
    clip_range_index = _build_clip_range_index(_clip_ranges(project, assets))
    slates_by_id = {str(item["id"]): item for item in project.get("syntheticSlates", [])}
    for block in video_blocks:
        if block.get("syntheticSlateId"):
            slate = slates_by_id[str(block["syntheticSlateId"])]
            video_slices.append(
                {
                    "id": _compiled_slice_id(
                        "vslice", block["id"], block["startUs"], block["endUs"], slate["id"]
                    ),
                    "blockId": block["id"],
                    "startUs": int(block["startUs"]),
                    "endUs": int(block["endUs"]),
                    "synthetic": True,
                    "slateId": slate["id"],
                    "slateText": slate["text"],
                    "transforms": ["generated no-footage slate"],
                    "provenance": slate["provenance"],
                }
            )
        else:
            video_slices.extend(
                _compile_source_block(
                    project, assets, block, issues, require_audio=False,
                    clip_range_index=clip_range_index,
                )
            )
    _interval_issues(audio_blocks, duration_us, "AUDIO", issues)
    audio_slices: list[dict[str, Any]] = []
    for block in audio_blocks:
        audio_slices.extend(
            _compile_audio_block(
                project, assets, block, video_slices, issues,
                clip_range_index=clip_range_index,
            )
        )
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
        "ConfirmSourceIdentities": _confirm_source_identities,
        "SetReferenceSource": _set_reference,
        "SetSyncTransform": lambda p, command_payload: _set_sync(p, command_payload, assets),
        "SetClipAlignment": lambda p, command_payload: _set_clip_alignment(
            p, command_payload, assets
        ),
        "AlignMarkedMoments": lambda p, command_payload: _align_marked_moments(
            p, command_payload, assets
        ),
        "AcceptAlignmentProposalSet": lambda p, command_payload: _accept_alignment_proposals(
            p, command_payload, assets
        ),
        "AcceptAlignmentProposal": lambda p, command_payload: _accept_alignment_proposals(
            p, command_payload, assets
        ),
        "RejectAlignmentProposal": lambda _p, _payload: None,
        "RejectAlignmentProposalSet": lambda _p, _payload: None,
        "SetClipProgramEligibility": _set_clip_program_eligibility,
        "SetRangeProgramEligibility": lambda p, command_payload: _set_range_program_eligibility(
            p, command_payload, assets
        ),
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
            "identityState": "USER_CONFIRMED",
            "confirmedAt": now_iso(),
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
    target_source["identityState"] = "USER_CONFIRMED"
    target_source["confirmedAt"] = now_iso()
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


def _confirm_source_identities(project: dict[str, Any], payload: dict[str, Any]) -> None:
    source_ids = list(dict.fromkeys(str(source_id) for source_id in payload.get("sourceIds", [])))
    if not source_ids:
        raise DomainError("VALIDATION_FAILED", "At least one logical source must be confirmed")
    confirmed_at = now_iso()
    for source_id in source_ids:
        source = _find(project["logicalSources"], source_id, "logical source")
        source["identityState"] = "USER_CONFIRMED"
        source["confirmedAt"] = confirmed_at


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


def _align_marked_moments(
    project: dict[str, Any], payload: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> None:
    reference = _find(project["clips"], payload["referenceClipId"], "reference project clip")
    target = _find(project["clips"], payload["targetClipId"], "target project clip")
    if reference["id"] == target["id"]:
        raise DomainError("VALIDATION_FAILED", "Marked moments require two different clips")
    reference_source_us = int(payload["referenceSourceUs"])
    target_source_us = int(payload["targetSourceUs"])
    reference_asset = assets.get(reference["assetId"], {})
    target_asset = assets.get(target["assetId"], {})
    reference_duration = _asset_duration_us(reference_asset)
    target_duration = _asset_duration_us(target_asset)
    if not 0 <= reference_source_us <= reference_duration:
        raise DomainError("VALIDATION_FAILED", "Reference moment is outside its source clip")
    if not 0 <= target_source_us <= target_duration:
        raise DomainError("VALIDATION_FAILED", "Target moment is outside its source clip")
    reference_alignment = _clip_alignment(reference)
    target_alignment = _clip_alignment(target)
    shared_aligned_us = reference_alignment.source_to_aligned(reference_source_us)
    _set_alignment(
        project,
        str(target["id"]),
        ClipAlignmentTransform(target_source_us, shared_aligned_us, target_alignment.rate_ppm),
        bool(target_alignment.rate_ppm),
        assets,
        legacy_payload=False,
        evidence=["manual", "marked-moment"],
        confidence=1.0,
    )


def _accept_alignment_proposals(
    project: dict[str, Any], payload: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> None:
    alignments = payload.get("alignments")
    if not isinstance(alignments, list) or not alignments:
        raise DomainError("VALIDATION_FAILED", "No canonical alignment proposals were selected")
    for item in alignments:
        if not isinstance(item, dict):
            raise DomainError("VALIDATION_FAILED", "Canonical alignment proposal is invalid")
        proposed = ClipAlignmentTransform.from_dict(item.get("alignment"))
        _set_alignment(
            project,
            str(item["clipId"]),
            proposed,
            bool(payload.get("confirmDrift")),
            assets,
            legacy_payload=False,
            evidence=[str(value) for value in item.get("evidenceKinds", ["manual-review"])],
            confidence=float(item.get("confidence", 0.0)),
        )


def _set_alignment(
    project: dict[str, Any],
    clip_id: str,
    new: ClipAlignmentTransform,
    confirm_drift: bool,
    assets: dict[str, dict[str, Any]],
    *,
    legacy_payload: bool,
    evidence: list[str] | None = None,
    confidence: float = 1.0,
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
        clip["alignmentConfidence"] = max(0.0, min(1.0, float(confidence)))
        clip["alignmentEvidence"] = list(evidence or ["manual"])
        if _program_eligibility(clip) != "EXCLUDED":
            clip["programEligibility"] = "ELIGIBLE"


def _set_clip_program_eligibility(project: dict[str, Any], payload: dict[str, Any]) -> None:
    eligibility = str(payload.get("programEligibility", ""))
    if eligibility not in PROGRAM_ELIGIBILITY_STATES:
        raise DomainError("VALIDATION_FAILED", "Unknown program eligibility")
    clip_ids = [str(value) for value in payload.get("clipIds", [])]
    if not clip_ids or len(clip_ids) != len(set(clip_ids)):
        raise DomainError("VALIDATION_FAILED", "clipIds must contain unique project clips")
    for clip_id in clip_ids:
        clip = _find(project["clips"], clip_id, "project clip")
        if eligibility == "ELIGIBLE" and _alignment_state(clip) != "ACCEPTED":
            raise DomainError("CLIP_NOT_ACCEPTED", "Only accepted clips can become eligible")
        clip["programEligibility"] = eligibility
        clip["programEligibilityRationale"] = str(payload.get("rationale", ""))[:1000]


def _set_range_program_eligibility(
    project: dict[str, Any], payload: dict[str, Any], assets: dict[str, dict[str, Any]]
) -> None:
    start_us = int(payload.get("startAlignedUs", 0))
    end_us = int(payload.get("endAlignedUs", 0))
    if end_us <= start_us:
        raise DomainError("VALIDATION_FAILED", "Eligibility range must have positive duration")
    source_ids = {str(value) for value in payload.get("sourceIds", [])}
    current = {str(value) for value in payload.get("currentEligibilityFilter", [])}
    clip_ids = [
        str(item["clipId"])
        for item in _clip_ranges(project, assets, include_provisional=True)
        if int(item["startUs"]) < end_us
        and int(item["endUs"]) > start_us
        and (not source_ids or str(item["logicalSourceId"]) in source_ids)
        and (
            not current
            or _program_eligibility(_find(project["clips"], str(item["clipId"]), "project clip"))
            in current
        )
    ]
    if not clip_ids:
        raise DomainError("VALIDATION_FAILED", "Eligibility range contains no matching clips")
    _set_clip_program_eligibility(project, {**payload, "clipIds": clip_ids})


def timeline_section_proposal(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    gap_mode: str = "EXCLUDE",
) -> dict[str, Any]:
    """Propose an explicit aligned-to-program composition without mutating state."""

    gap_mode = str(gap_mode).upper()
    if gap_mode not in {"EXCLUDE", "SLATE"}:
        raise DomainError("VALIDATION_FAILED", "gapMode must be EXCLUDE or SLATE")
    accepted = _union_intervals(
        (int(item["startUs"]), int(item["endUs"]))
        for item in _clip_ranges(project, assets, eligible_only=True)
    )
    if not accepted:
        raise DomainError("COVERAGE_INVALID", "No accepted aligned media is available for composition")
    evidence = _union_intervals(
        (int(item["startUs"]), int(item["endUs"]))
        for item in _clip_ranges(project, assets, include_provisional=True)
    )
    evidence_start_us = min(start_us for start_us, _end_us in evidence)
    evidence_end_us = max(end_us for _start_us, end_us in evidence)
    sections: list[dict[str, Any]] = []

    def append_section(start_us: int, end_us: int, mode: str) -> None:
        if end_us <= start_us:
            return
        identity = digest_json(
            {
                "projectId": project["id"],
                "revision": project["revision"],
                "startAlignedUs": start_us,
                "endAlignedUs": end_us,
                "mode": mode,
            }
        )[:24]
        sections.append(
            {
                "id": f"section_{identity}",
                "startAlignedUs": start_us,
                "endAlignedUs": end_us,
                "mode": mode,
                "slateText": "No recorded footage" if mode == "SLATE" else None,
            }
        )

    cursor = evidence_start_us
    for start_us, end_us in accepted:
        append_section(cursor, start_us, gap_mode)
        append_section(start_us, end_us, "KEEP")
        cursor = end_us
    append_section(cursor, evidence_end_us, gap_mode)
    keep_us = sum(
        int(item["endAlignedUs"]) - int(item["startAlignedUs"])
        for item in sections
        if item["mode"] == "KEEP"
    )
    gap_us = sum(
        int(item["endAlignedUs"]) - int(item["startAlignedUs"])
        for item in sections
        if item["mode"] != "KEEP"
    )
    output_us = keep_us + (gap_us if gap_mode == "SLATE" else 0)
    value = {
        "projectId": project["id"],
        "projectRevision": int(project["revision"]),
        "alignmentDigest": alignment_digest(project),
        "gapMode": gap_mode,
        "sections": sections,
        "keepDurationUs": keep_us,
        "excludedDurationUs": gap_us if gap_mode == "EXCLUDE" else 0,
        "slateDurationUs": gap_us if gap_mode == "SLATE" else 0,
        "outputDurationUs": output_us,
    }
    value["digest"] = digest_json(value)
    return value


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
        if end_us <= start_us:
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


def _program_section_mappings(sections: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = 0
    mappings: list[dict[str, Any]] = []
    for section in sorted(sections, key=lambda item: (int(item["startAlignedUs"]), item["id"])):
        duration_us = int(section["endAlignedUs"]) - int(section["startAlignedUs"])
        if section["mode"] == "EXCLUDE":
            continue
        mappings.append(
            {
                **section,
                "startProgramUs": cursor,
                "endProgramUs": cursor + duration_us,
            }
        )
        cursor += duration_us
    return mappings


def _optimize_keep_section(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    section: dict[str, Any],
    start_program_us: int,
    clip_range_index: dict[str, tuple[list[int], list[int], list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    start_aligned_us = int(section["startAlignedUs"])
    end_aligned_us = int(section["endAlignedUs"])
    clip_ranges: list[dict[str, Any]] = []
    for source_id in clip_range_index:
        clip_ranges.extend(
            _overlapping_clip_ranges(
                clip_range_index, source_id, start_aligned_us, end_aligned_us
            )
        )
    boundaries = {start_aligned_us, end_aligned_us}
    for item in clip_ranges:
        boundaries.add(max(start_aligned_us, int(item["startUs"])))
        boundaries.add(min(end_aligned_us, int(item["endUs"])))
    points = sorted(boundaries)
    clip_by_id = {str(item["id"]): item for item in project.get("clips", [])}
    starting: dict[int, list[dict[str, Any]]] = {}
    ending: dict[int, list[dict[str, Any]]] = {}
    for item in clip_ranges:
        clipped_start = max(start_aligned_us, int(item["startUs"]))
        clipped_end = min(end_aligned_us, int(item["endUs"]))
        if clipped_end <= clipped_start:
            continue
        starting.setdefault(clipped_start, []).append(item)
        ending.setdefault(clipped_end, []).append(item)
    active_by_source: dict[str, dict[str, dict[str, Any]]] = {}
    intervals: list[dict[str, Any]] = []
    for interval_start, interval_end in zip(points, points[1:]):
        if interval_end <= interval_start:
            continue
        for item in ending.get(interval_start, []):
            source_items = active_by_source.get(str(item["logicalSourceId"]))
            if source_items is not None:
                source_items.pop(str(item["clipId"]), None)
                if not source_items:
                    active_by_source.pop(str(item["logicalSourceId"]), None)
        for item in starting.get(interval_start, []):
            active_by_source.setdefault(str(item["logicalSourceId"]), {})[
                str(item["clipId"])
            ] = item
        candidates: dict[str, dict[str, Any]] = {}
        for source_id, active in active_by_source.items():
            covering = list(active.values())
            ranked = []
            for clip_range in covering:
                clip = clip_by_id[str(clip_range["clipId"])]
                ranked.append(
                    (
                        not _asset_has_audio(assets.get(str(clip["assetId"]), {})),
                        -float(
                            clip.get(
                                "alignmentConfidence", 1.0 if "sync" in clip else 0.0
                            )
                        ),
                        bool(_clip_alignment(clip).rate_ppm),
                        str(clip["id"]),
                        clip,
                    )
                )
            _audio_rank, _confidence_rank, _transform_rank, _clip_id, clip = min(ranked)
            candidates[source_id] = {
                "clipId": clip["id"],
                "hasAudio": _asset_has_audio(
                    assets.get(str(clip["assetId"]), {})
                ),
                "confidence": float(clip.get("alignmentConfidence", 1.0 if "sync" in clip else 0.0)),
                "transformed": bool(_clip_alignment(clip).rate_ppm),
                "resolvedOverlap": len(covering) > 1,
            }
        if not candidates:
            raise DomainError(
                "COVERAGE_INVALID",
                "A kept interval has no unambiguous accepted video source",
                {
                    "startAlignedUs": interval_start,
                    "endAlignedUs": interval_end,
                },
            )
        intervals.append(
            {
                "startAlignedUs": interval_start,
                "endAlignedUs": interval_end,
                "candidates": candidates,
            }
        )
    if not intervals:
        raise DomainError("COVERAGE_INVALID", "A kept section has no accepted video coverage")

    layers: list[dict[str, tuple[tuple[int, int, int, int], str | None]]] = []
    for interval_index, interval in enumerate(intervals):
        duration_us = int(interval["endAlignedUs"]) - int(interval["startAlignedUs"])
        layer: dict[str, tuple[tuple[int, int, int, int], str | None]] = {}
        for source_id, candidate in sorted(interval["candidates"].items()):
            audio_cost = 0 if candidate["hasAudio"] else duration_us
            confidence_cost = -round(float(candidate["confidence"]) * duration_us)
            transform_cost = duration_us if candidate["transformed"] else 0
            if interval_index == 0:
                layer[source_id] = (
                    (audio_cost, confidence_cost, 0, transform_cost),
                    None,
                )
                continue
            options = []
            for previous_source, (previous_cost, _predecessor) in layers[-1].items():
                cost = (
                    previous_cost[0] + audio_cost,
                    previous_cost[1] + confidence_cost,
                    previous_cost[2] + int(previous_source != source_id),
                    previous_cost[3] + transform_cost,
                )
                options.append((cost, previous_source))
            layer[source_id] = min(options, key=lambda item: (item[0], item[1]))
        layers.append(layer)
    selected_sources = [""] * len(intervals)
    selected_sources[-1] = min(layers[-1], key=lambda source_id: (layers[-1][source_id][0], source_id))
    for index in range(len(intervals) - 1, 0, -1):
        predecessor = layers[index][selected_sources[index]][1]
        if predecessor is None:
            raise DomainError("INTERNAL_ERROR", "Coverage optimizer lost its predecessor")
        selected_sources[index - 1] = predecessor

    blocks: list[dict[str, Any]] = []
    for index, (interval, source_id) in enumerate(zip(intervals, selected_sources)):
        start_us = start_program_us + int(interval["startAlignedUs"]) - start_aligned_us
        end_us = start_program_us + int(interval["endAlignedUs"]) - start_aligned_us
        candidate = interval["candidates"][source_id]
        reason = (
            "deterministic-clip-selection"
            if candidate["resolvedOverlap"]
            else "coverage-continuity"
            if blocks and blocks[-1]["logicalSourceId"] == source_id
            else "higher-alignment-confidence"
            if candidate["confidence"] >= 0.9
            else "usable-unambiguous-coverage"
        )
        if (
            blocks
            and blocks[-1]["endUs"] == start_us
            and blocks[-1]["logicalSourceId"] == source_id
            and blocks[-1]["pinnedClipId"] == candidate["clipId"]
        ):
            blocks[-1]["endUs"] = end_us
            blocks[-1]["endAlignedUs"] = int(interval["endAlignedUs"])
            continue
        blocks.append(
            {
                "id": opaque_id("vblock"),
                "startUs": start_us,
                "endUs": end_us,
                "logicalSourceId": source_id,
                "pinnedClipId": candidate["clipId"],
                "syntheticSlateId": None,
                "sectionId": section["id"],
                "startAlignedUs": int(interval["startAlignedUs"]),
                "endAlignedUs": int(interval["endAlignedUs"]),
                "decisionReason": reason,
            }
        )
    return blocks


def generate_program_draft(
    project: dict[str, Any], assets: dict[str, dict[str, Any]], payload: dict[str, Any]
) -> dict[str, Any]:
    current_alignment_digest = alignment_digest(project)
    if str(payload.get("alignmentDigest", "")) != current_alignment_digest:
        raise DomainError("PLAN_STALE", "Alignment changed after the program draft was prepared")
    selection = project.get("selectionSnapshot") or {}
    if str(payload.get("selectionDigest", "")) != str(selection.get("digest", "")):
        raise DomainError("PLAN_STALE", "Project selection changed after the program draft was prepared")
    if project.get("videoBlocks") and not payload.get("replaceExisting"):
        raise DomainError("VALIDATION_FAILED", "replaceExisting is required to replace an existing program")
    provisional_sources = [
        source["id"]
        for source in project.get("logicalSources", [])
        if not source.get("archived")
        and source.get("identityState", "USER_CONFIRMED") != "USER_CONFIRMED"
    ]
    if provisional_sources:
        raise DomainError(
            "COVERAGE_INVALID",
            "Source identities must be confirmed before a first cut is generated",
            {"provisionalSourceIds": provisional_sources},
        )
    result = copy.deepcopy(project)
    gap_mode = payload.get("gapMode")
    if gap_mode is not None:
        proposal = timeline_section_proposal(project, assets, str(gap_mode))
        if str(payload.get("sectionProposalDigest", "")) != str(proposal["digest"]):
            raise DomainError(
                "PLAN_STALE",
                "Composition proposal changed before the program draft was generated",
            )
        _set_timeline_sections(result, {"sections": proposal["sections"]})
    readiness = alignment_summary(result, assets)
    if not readiness["readyForProgramDraft"]:
        raise DomainError(
            "COVERAGE_INVALID",
            "Accepted alignment does not yet cover every required interval",
            {
                "unresolvedSoleCoverageUs": readiness["coverage"]["unresolvedSoleCoverageUs"],
                "conflicts": readiness["conflicts"],
            },
        )
    sections = result.get("timelineSections", [])
    if not sections:
        raise DomainError(
            "COVERAGE_INVALID",
            "Explicit keep, exclude, or slate decisions are required before program generation",
        )
    mappings = _program_section_mappings(sections)
    result["videoBlocks"] = []
    result["audioBlocks"] = []
    result["syntheticSlates"] = []
    clip_range_index = _build_clip_range_index(_clip_ranges(result, assets, eligible_only=True))
    for mapping in mappings:
        if mapping["mode"] == "SLATE":
            slate_id = opaque_id("slate")
            result["syntheticSlates"].append(
                {
                    "id": slate_id,
                    "text": mapping.get("slateText") or "No recorded footage",
                    "videoGenerated": True,
                    "audioMode": "SILENCE",
                    "provenance": {
                        "sectionId": mapping["id"],
                        "startAlignedUs": mapping["startAlignedUs"],
                        "endAlignedUs": mapping["endAlignedUs"],
                        "decision": "SLATE",
                    },
                }
            )
            result["videoBlocks"].append(
                {
                    "id": opaque_id("vblock"),
                    "startUs": mapping["startProgramUs"],
                    "endUs": mapping["endProgramUs"],
                    "logicalSourceId": None,
                    "pinnedClipId": None,
                    "syntheticSlateId": slate_id,
                    "sectionId": mapping["id"],
                    "startAlignedUs": mapping["startAlignedUs"],
                    "endAlignedUs": mapping["endAlignedUs"],
                    "decisionReason": "explicit-no-footage-slate",
                }
            )
            audio_mode = "SILENCE"
        else:
            result["videoBlocks"].extend(
                _optimize_keep_section(
                    result,
                    assets,
                    mapping,
                    int(mapping["startProgramUs"]),
                    clip_range_index,
                )
            )
            audio_mode = "FOLLOW_VIDEO"
        result["audioBlocks"].append(
            {
                "id": opaque_id("ablock"),
                "startUs": mapping["startProgramUs"],
                "endUs": mapping["endProgramUs"],
                "mode": audio_mode,
                "logicalSourceId": None,
                "clipId": None,
                "offsetUs": 0,
                "ratePpm": 0,
            }
        )
    output_duration_us = max((int(item["endUs"]) for item in result["videoBlocks"]), default=0)
    if output_duration_us <= 0:
        raise DomainError("COVERAGE_INVALID", "Composition decisions produce no output")
    result["programDraft"] = {
        "id": opaque_id("draft"),
        "selectionDigest": selection.get("digest"),
        "alignmentDigest": current_alignment_digest,
        "timelineSectionsDigest": digest_json(sections),
        "sectionProposalDigest": payload.get("sectionProposalDigest"),
        "gapMode": gap_mode,
        "generatedAt": now_iso(),
        "strategy": "coverage-optimizer-v1",
        "outputDurationUs": output_duration_us,
        "sourceChanges": sum(
            left.get("logicalSourceId") != right.get("logicalSourceId")
            for left, right in zip(result["videoBlocks"], result["videoBlocks"][1:])
        ),
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
    original_start = int(block["startUs"])
    original_end = int(block["endUs"])
    aligned_start, aligned_end = _block_aligned_bounds(project, block)
    aligned_at = aligned_start + at - original_start
    block["endUs"] = at
    if block.get("startAlignedUs") is not None:
        block["endAlignedUs"] = aligned_at
    project["videoBlocks"].append(
        {
            **copy.deepcopy(block),
            "id": payload.get("newBlockId") or opaque_id("vblock"),
            "startUs": at,
            "endUs": original_end,
            **(
                {"startAlignedUs": aligned_at, "endAlignedUs": aligned_end}
                if block.get("startAlignedUs") is not None
                else {}
            ),
        }
    )


def _move_video_boundary(project: dict[str, Any], payload: dict[str, Any]) -> None:
    left = _find(project["videoBlocks"], payload["leftBlockId"], "video block")
    right = _find(project["videoBlocks"], payload["rightBlockId"], "video block")
    at = int(payload["atUs"])
    if at <= int(left["startUs"]) or at >= int(right["endUs"]):
        raise DomainError("VALIDATION_FAILED", "Boundary must remain inside adjacent blocks")
    if left.get("sectionId") and right.get("sectionId") and left["sectionId"] != right["sectionId"]:
        raise DomainError(
            "VALIDATION_FAILED",
            "A boundary cannot move across an explicit composition-section boundary",
        )
    left_aligned_start, _left_aligned_end = _block_aligned_bounds(project, left)
    _right_aligned_start, right_aligned_end = _block_aligned_bounds(project, right)
    aligned_at = left_aligned_start + at - int(left["startUs"])
    left["endUs"] = at
    right["startUs"] = at
    if left.get("startAlignedUs") is not None:
        left["endAlignedUs"] = aligned_at
    if right.get("startAlignedUs") is not None:
        right["startAlignedUs"] = aligned_at
        right["endAlignedUs"] = right_aligned_end


def _delete_video_block(project: dict[str, Any], payload: dict[str, Any]) -> None:
    project["videoBlocks"] = [item for item in project["videoBlocks"] if item["id"] != payload["blockId"]]


def _assign_video_source(project: dict[str, Any], payload: dict[str, Any]) -> None:
    _find(project["logicalSources"], payload["logicalSourceId"], "logical source")
    block = _find(project["videoBlocks"], payload["blockId"], "video block")
    if block.get("syntheticSlateId"):
        raise DomainError(
            "VALIDATION_FAILED", "Change the timeline section before replacing a deliberate slate"
        )
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
    eligible_only: bool = False,
    include_unavailable: bool = False,
) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for clip in project.get("clips", []):
        asset = assets.get(clip["assetId"])
        if not asset or (asset.get("missing") and not include_unavailable):
            continue
        state = clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED")
        if state == "UNRESOLVED" or (not include_provisional and state != "ACCEPTED"):
            continue
        if eligible_only and _program_eligibility(clip) != "ELIGIBLE":
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


def _build_clip_range_index(
    ranges: Iterable[dict[str, Any]],
) -> dict[str, tuple[list[int], list[int], list[dict[str, Any]]]]:
    """Build a source-local interval index once per project revision.

    `prefix_max_end` makes overlap lookup logarithmic plus returned matches,
    including when long clips overlap many later clip starts.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in ranges:
        grouped.setdefault(str(item["logicalSourceId"]), []).append(item)
    index: dict[str, tuple[list[int], list[int], list[dict[str, Any]]]] = {}
    for source_id, items in grouped.items():
        ordered = sorted(items, key=lambda item: (int(item["startUs"]), int(item["endUs"]), item["clipId"]))
        starts = [int(item["startUs"]) for item in ordered]
        prefix_max_end: list[int] = []
        current_end = -(2**63)
        for item in ordered:
            current_end = max(current_end, int(item["endUs"]))
            prefix_max_end.append(current_end)
        index[source_id] = (starts, prefix_max_end, ordered)
    return index


def _overlapping_clip_ranges(
    index: dict[str, tuple[list[int], list[int], list[dict[str, Any]]]],
    source_id: str,
    start_us: int,
    end_us: int,
) -> list[dict[str, Any]]:
    entry = index.get(str(source_id))
    if not entry or end_us <= start_us:
        return []
    starts, prefix_max_end, ordered = entry
    first = bisect_right(prefix_max_end, int(start_us))
    stop = bisect_left(starts, int(end_us))
    return [item for item in ordered[first:stop] if int(item["endUs"]) > int(start_us)]


def _block_aligned_bounds(project: dict[str, Any], block: dict[str, Any]) -> tuple[int, int]:
    if block.get("startAlignedUs") is not None and block.get("endAlignedUs") is not None:
        return int(block["startAlignedUs"]), int(block["endAlignedUs"])
    start_program_us = int(block["startUs"])
    end_program_us = int(block["endUs"])
    for mapping in _program_section_mappings(project.get("timelineSections", [])):
        if (
            mapping["mode"] == "KEEP"
            and int(mapping["startProgramUs"]) <= start_program_us
            and int(mapping["endProgramUs"]) >= end_program_us
        ):
            return (
                int(mapping["startAlignedUs"]) + start_program_us - int(mapping["startProgramUs"]),
                int(mapping["startAlignedUs"]) + end_program_us - int(mapping["startProgramUs"]),
            )
    # Legacy projects used one clock. Keeping this fallback explicit preserves
    # their existing decisions until the user confirms a repair revision.
    return start_program_us, end_program_us


def _compile_source_block(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    block: dict[str, Any],
    issues: list[dict[str, Any]],
    require_audio: bool,
    clip_range_index: dict[str, tuple[list[int], list[int], list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    block_start_aligned_us, block_end_aligned_us = _block_aligned_bounds(project, block)
    ranges = [
        item
        for item in _overlapping_clip_ranges(
            clip_range_index,
            str(block["logicalSourceId"]),
            block_start_aligned_us,
            block_end_aligned_us,
        )
        if not block.get("pinnedClipId") or item["clipId"] == block["pinnedClipId"]
    ]
    boundaries = {block_start_aligned_us, block_end_aligned_us}
    for item in ranges:
        if item["endUs"] > block_start_aligned_us and item["startUs"] < block_end_aligned_us:
            boundaries.add(max(block_start_aligned_us, int(item["startUs"])))
            boundaries.add(min(block_end_aligned_us, int(item["endUs"])))
    points = sorted(boundaries)
    starting: dict[int, list[dict[str, Any]]] = {}
    ending: dict[int, list[dict[str, Any]]] = {}
    for item in ranges:
        clipped_start = max(block_start_aligned_us, int(item["startUs"]))
        clipped_end = min(block_end_aligned_us, int(item["endUs"]))
        if clipped_end <= clipped_start:
            continue
        starting.setdefault(clipped_start, []).append(item)
        ending.setdefault(clipped_end, []).append(item)
    active: dict[str, dict[str, Any]] = {}
    slices: list[dict[str, Any]] = []
    for start_aligned_us, end_aligned_us in zip(points, points[1:]):
        if end_aligned_us <= start_aligned_us:
            continue
        for item in ending.get(start_aligned_us, []):
            active.pop(str(item["clipId"]), None)
        for item in starting.get(start_aligned_us, []):
            active[str(item["clipId"])] = item
        candidates = list(active.values())
        start_us = int(block["startUs"]) + start_aligned_us - block_start_aligned_us
        end_us = int(block["startUs"]) + end_aligned_us - block_start_aligned_us
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
        transform: ClipAlignmentTransform = selected["transform"]
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
                "sourceStartUs": transform.aligned_to_source(start_aligned_us),
                "sourceEndUs": transform.aligned_to_source(end_aligned_us),
                "sync": transform.to_legacy_sync_dict(),
                "alignment": transform.to_dict(),
                "startAlignedUs": start_aligned_us,
                "endAlignedUs": end_aligned_us,
            }
        )
    return slices


def _compile_audio_block(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    block: dict[str, Any],
    video_slices: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    clip_range_index: dict[str, tuple[list[int], list[int], list[dict[str, Any]]]],
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
            if video.get("synthetic"):
                issues.append(
                    _issue(
                        "AUDIO_UNAVAILABLE",
                        start_us,
                        end_us,
                        "FOLLOW_VIDEO cannot derive recorded audio from a generated slate",
                        blockId=block["id"],
                    )
                )
                continue
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
            program_span_us = int(video["endUs"]) - int(video["startUs"])
            source_span_us = int(video["sourceEndUs"]) - int(video["sourceStartUs"])
            if program_span_us <= 0 or source_span_us <= 0:
                issues.append(
                    _issue(
                        "AUDIO_UNAVAILABLE",
                        start_us,
                        end_us,
                        "FOLLOW_VIDEO selected a slice with an invalid time mapping",
                        blockId=block["id"],
                        assetId=video["assetId"],
                    )
                )
                continue
            source_start_us = int(video["sourceStartUs"]) + _round_ratio(
                (start_us - int(video["startUs"])) * source_span_us,
                program_span_us,
            )
            source_end_us = int(video["sourceStartUs"]) + _round_ratio(
                (end_us - int(video["startUs"])) * source_span_us,
                program_span_us,
            )
            aligned_span_us = int(video.get("endAlignedUs", video["endUs"])) - int(
                video.get("startAlignedUs", video["startUs"])
            )
            aligned_start_us = int(video.get("startAlignedUs", video["startUs"])) + _round_ratio(
                (start_us - int(video["startUs"])) * aligned_span_us,
                program_span_us,
            )
            aligned_end_us = int(video.get("startAlignedUs", video["startUs"])) + _round_ratio(
                (end_us - int(video["startUs"])) * aligned_span_us,
                program_span_us,
            )
            slices.append(
                {
                    **copy.deepcopy(video),
                    "id": _compiled_slice_id(
                        "aslice", block["id"], start_us, end_us, video["assetId"], video["clipId"]
                    ),
                    "blockId": block["id"],
                    "startUs": start_us,
                    "endUs": end_us,
                    "startAlignedUs": aligned_start_us,
                    "endAlignedUs": aligned_end_us,
                    "sourceStartUs": source_start_us,
                    "sourceEndUs": source_end_us,
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
    slices = _compile_source_block(
        project, assets, synthetic_block, issues, require_audio=True,
        clip_range_index=clip_range_index,
    )
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
    for item in slices:
        # Source ranges have already been resolved from aligned evidence by the
        # video/source compiler. Independent audio timing must transform that
        # exact range; feeding programTimeUs back through the clip alignment
        # would incorrectly reintroduce excluded evidence gaps.
        base_source_start_us = int(item["sourceStartUs"])
        base_source_end_us = int(item["sourceEndUs"])
        base_source_duration_us = base_source_end_us - base_source_start_us
        item["sourceStartUs"] = base_source_start_us + offset_us
        item["sourceEndUs"] = item["sourceStartUs"] + _round_ratio(
            base_source_duration_us * 1_000_000,
            rate_denominator,
        )
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
