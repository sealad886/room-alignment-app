from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from room_alignment import __version__


EvidenceKind = Literal["filesystem", "filename", "container", "sidecar", "importer", "user"]


@dataclass(slots=True)
class ProvenanceEvidence:
    kind: EvidenceKind
    field: str
    value: Any
    confidence: float
    origin: str
    raw_value: Any | None = None
    normalized_value: Any | None = None
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    extractor: str = "room-alignment"
    extractor_version: str = __version__
    uncertainty: str | None = None
    custom: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MediaRecord:
    id: str
    library_id: str
    relative_path: str
    size: int
    modified_ns: int
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    captured_at: str | None = None
    camera: str | None = None
    sequence: str | None = None
    evidence: list[ProvenanceEvidence] = field(default_factory=list)
    custom: dict[str, Any] = field(default_factory=dict)
    warning: str | None = None
    duration_us: int | None = None
    streams: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: dict[str, Any] = field(default_factory=dict)
    source_candidate_id: str | None = None
    missing: bool = False
    generation: int = 0
    root_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["durationUs"] = value.pop("duration_us")
        value["sourceCandidateId"] = value.pop("source_candidate_id")
        value["rootId"] = value.pop("root_id")
        return value


@dataclass(slots=True)
class ScanSummary:
    library_id: str
    root: str
    scanned: int
    videos: int
    warnings: int
    cameras: list[str]
    date_groups: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
