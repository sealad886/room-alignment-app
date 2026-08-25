from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


EvidenceKind = Literal["filesystem", "filename", "container", "sidecar", "importer", "user"]


@dataclass(slots=True)
class ProvenanceEvidence:
    kind: EvidenceKind
    field: str
    value: Any
    confidence: float
    origin: str


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

