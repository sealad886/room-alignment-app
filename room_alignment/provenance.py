from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ProvenanceEvidence


DATE_PATTERNS = (
    re.compile(r"(?P<date>20\d{2}[-_.]?\d{2}[-_.]?\d{2})[T _-]?(?P<time>\d{2}[-_.:]?\d{2}[-_.:]?\d{2})"),
    re.compile(r"(?P<date>\d{2}[-_.]\d{2}[-_.]20\d{2})[T _-]?(?P<time>\d{2}[-_.:]\d{2}[-_.:]\d{2})"),
)
DATE_ONLY = re.compile(r"(?<!\d)(?P<year>20\d{2}|\d{2})[-_.](?P<month>\d{2})[-_.](?P<day>\d{2})(?!\d)")
TIME_ONLY = re.compile(r"(?<!\d)(?P<hour>\d{2})[-_.:](?P<minute>\d{2})[-_.:](?P<second>\d{2})(?!\d)")
GENERIC_TOKENS = {
    "clip", "video", "camera", "cam", "motion", "recording", "record", "event", "mp4", "mov", "mkv"
}


def _parse_datetime(raw_date: str, raw_time: str) -> str | None:
    digits_date = re.sub(r"\D", "", raw_date)
    digits_time = re.sub(r"\D", "", raw_time)
    candidates = []
    if len(digits_date) == 8 and digits_date.startswith("20"):
        candidates.append(digits_date + digits_time)
    elif len(digits_date) == 8:
        candidates.append(digits_date[4:] + digits_date[2:4] + digits_date[:2] + digits_time)
    for value in candidates:
        try:
            return datetime.strptime(value[:14], "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            continue
    return None


def infer_from_path(path: Path, relative: Path) -> tuple[dict[str, Any], list[ProvenanceEvidence]]:
    values: dict[str, Any] = {}
    evidence: list[ProvenanceEvidence] = []
    stem = path.stem
    matched_text = ""
    for pattern in DATE_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        matched_text = match.group(0)
        captured = _parse_datetime(match.group("date"), match.group("time"))
        if captured:
            values["captured_at"] = captured
            evidence.append(ProvenanceEvidence("filename", "captured_at", captured, 0.8, relative.as_posix()))
        break
    if "captured_at" not in values:
        date_match = None
        for parent in relative.parents:
            date_match = DATE_ONLY.search(parent.name)
            if date_match:
                break
        time_match = TIME_ONLY.search(stem)
        if date_match and time_match:
            year = int(date_match.group("year"))
            year = 2000 + year if year < 100 else year
            try:
                captured = datetime(
                    year, int(date_match.group("month")), int(date_match.group("day")),
                    int(time_match.group("hour")), int(time_match.group("minute")), int(time_match.group("second")),
                ).isoformat()
                values["captured_at"] = captured
                evidence.append(ProvenanceEvidence("filesystem", "captured_at.date", captured[:10], 0.65, parent.as_posix()))
                evidence.append(ProvenanceEvidence("filename", "captured_at.time", captured[11:], 0.7, relative.as_posix()))
                evidence.append(ProvenanceEvidence("importer", "captured_at", captured, 0.68, "folder-date + filename-time"))
                matched_text = time_match.group(0)
            except ValueError:
                pass

    sequence_matches = re.findall(r"(?:^|[-_. ])(\d{3,})(?:$|[-_. ])", stem)
    if sequence_matches:
        values["sequence"] = sequence_matches[-1]
        evidence.append(ProvenanceEvidence("filename", "sequence", values["sequence"], 0.65, relative.as_posix()))

    remaining = stem.replace(matched_text, " ") if matched_text else stem
    parts = [p for p in re.split(r"[-_. ]+", remaining) if p]
    camera_parts = [p for p in parts if not p.isdigit() and p.lower() not in GENERIC_TOKENS]
    if camera_parts:
        candidate = " ".join(camera_parts[-2:]).strip()
        human_prefix = re.match(r"^(.*?)(?=[A-Z]\d[A-Za-z0-9]{5,}$)", candidate)
        camera = (human_prefix.group(1) if human_prefix and human_prefix.group(1) else candidate).strip().title()
        values["camera"] = camera
        evidence.append(ProvenanceEvidence("filename", "camera", camera, 0.55, relative.as_posix()))
    elif relative.parent.name and relative.parent.name not in {".", ".."}:
        camera = relative.parent.name.replace("_", " ").replace("-", " ").title()
        values["camera"] = camera
        evidence.append(ProvenanceEvidence("filesystem", "camera", camera, 0.35, relative.parent.as_posix()))
    return values, evidence


def read_sidecar(path: Path, relative: Path) -> tuple[dict[str, Any], list[ProvenanceEvidence]]:
    candidates = [path.with_suffix(path.suffix + ".json"), path.with_suffix(".json")]
    for candidate in candidates:
        if not candidate.is_file() or candidate.stat().st_size > 1_000_000:
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        aliases = {
            "captured_at": ("captured_at", "captureTime", "created_at", "timestamp", "dateTimeOriginal"),
            "camera": ("camera", "camera_name", "deviceName", "source", "location"),
            "sequence": ("sequence", "sequence_id", "eventId"),
        }
        values: dict[str, Any] = {}
        evidence: list[ProvenanceEvidence] = []
        for field, keys in aliases.items():
            for key in keys:
                if raw.get(key) not in (None, ""):
                    values[field] = raw[key]
                    evidence.append(ProvenanceEvidence("sidecar", field, raw[key], 0.9, candidate.name))
                    break
        known = {key for keys in aliases.values() for key in keys}
        custom = {k: v for k, v in raw.items() if k not in known}
        if custom:
            values["custom"] = custom
        return values, evidence
    return {}, []


def merge_evidence(*sources: tuple[dict[str, Any], list[ProvenanceEvidence]]) -> tuple[dict[str, Any], list[ProvenanceEvidence]]:
    merged: dict[str, Any] = {}
    chosen_confidence: dict[str, float] = {}
    all_evidence: list[ProvenanceEvidence] = []
    for values, evidence in sources:
        all_evidence.extend(evidence)
        confidence = {item.field: item.confidence for item in evidence}
        for key, value in values.items():
            if key == "custom":
                merged.setdefault("custom", {}).update(value if isinstance(value, dict) else {})
                continue
            score = confidence.get(key, 0.5)
            if key not in merged or score >= chosen_confidence.get(key, -1):
                merged[key] = value
                chosen_confidence[key] = score
    return merged, all_evidence
