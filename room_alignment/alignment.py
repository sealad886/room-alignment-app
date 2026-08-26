from __future__ import annotations

import cmath
import hashlib
import json
import math
import os
import signal
import struct
import subprocess
import tempfile
import time
from array import array
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import ClipAlignmentTransform, DomainError, digest_json, now_iso, opaque_id


SIGNATURE_VERSION = 1
SIGNATURE_SAMPLE_RATE = 400
SIGNATURE_MAX_SECONDS = 15 * 60
SIGNATURE_MAGIC = b"RASIG1\0"
DEFAULT_UNCERTAINTY_US = 30_000_000
DEFAULT_MAX_CANDIDATES_PER_CLIP = 8
DEFAULT_MAX_PAIR_COMPARISONS = 2_000
MAX_TIMELINE_ITEMS = 2_000


class AlignmentCanceled(RuntimeError):
    pass


class SignatureUnavailable(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AudioSignature:
    key: str
    sample_rate: int
    samples: Sequence[int]
    truncated: bool

    @property
    def duration_us(self) -> int:
        return round(len(self.samples) * 1_000_000 / self.sample_rate)


@dataclass(frozen=True, slots=True)
class CorrelationEvidence:
    correction_us: int
    confidence: float
    envelope_score: float
    envelope_peak_ratio: float
    phat_peak_ratio: float
    confirmation_delta_us: int
    overlap_us: int


def _asset_duration_us(asset: dict[str, Any]) -> int:
    if asset.get("durationUs") is not None:
        return int(asset["durationUs"])
    if asset.get("duration") is not None:
        return round(float(asset["duration"]) * 1_000_000)
    return 0


def _has_audio(asset: dict[str, Any]) -> bool:
    return bool(asset.get("audio_codec")) or any(
        item.get("codecType") == "audio" for item in asset.get("streams", [])
    )


def signature_key(asset: dict[str, Any]) -> str:
    audio_streams = [
        {
            key: stream.get(key)
            for key in ("id", "index", "codecName", "timeBase", "sampleRate", "channels")
        }
        for stream in asset.get("streams", [])
        if stream.get("codecType") == "audio"
    ]
    return digest_json(
        {
            "version": SIGNATURE_VERSION,
            "sampleRate": SIGNATURE_SAMPLE_RATE,
            "maxSeconds": SIGNATURE_MAX_SECONDS,
            "assetId": asset.get("id"),
            "fingerprint": asset.get("fingerprint", {}),
            "audioStreams": audio_streams,
        }
    )


class AudioSignatureCache:
    """Content-addressed, bounded audio evidence outside source libraries."""

    def __init__(self, store: Any):
        self.store = store
        self.root = (store.path.parent / "cache" / "audio-signatures").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def signature(
        self,
        asset: dict[str, Any],
        canceled: Callable[[], bool] | None = None,
    ) -> AudioSignature:
        if not _has_audio(asset):
            raise SignatureUnavailable("NO_AUDIO_STREAM")
        key = signature_key(asset)
        cached = self._read(key)
        if cached is not None:
            self.store.touch_cache_entry(f"audio-signature:{key}")
            return cached
        if canceled and canceled():
            raise AlignmentCanceled("Alignment analysis canceled")
        source = self.store.media_source_path(str(asset["id"]))
        samples, truncated = self._extract(source, _asset_duration_us(asset), canceled)
        if not samples:
            raise SignatureUnavailable("AUDIO_DECODE_EMPTY")
        signature = AudioSignature(key, SIGNATURE_SAMPLE_RATE, samples, truncated)
        self._write(signature)
        return signature

    def cached_waveform(
        self,
        asset: dict[str, Any],
        start_us: int = 0,
        end_us: int | None = None,
        max_points: int = MAX_TIMELINE_ITEMS,
    ) -> dict[str, Any]:
        key = signature_key(asset)
        signature = self._read(key)
        if signature is None:
            return {
                "assetId": asset["id"],
                "available": False,
                "reason": "ANALYSIS_REQUIRED",
                "points": [],
            }
        self.store.touch_cache_entry(f"audio-signature:{key}")
        end_us = signature.duration_us if end_us is None else min(int(end_us), signature.duration_us)
        start_us = max(0, int(start_us))
        if end_us <= start_us:
            raise DomainError("VALIDATION_FAILED", "Waveform range must have positive duration")
        start_index = max(0, start_us * signature.sample_rate // 1_000_000)
        end_index = min(
            len(signature.samples),
            math.ceil(end_us * signature.sample_rate / 1_000_000),
        )
        selected = signature.samples[start_index:end_index]
        point_count = max(1, min(int(max_points), MAX_TIMELINE_ITEMS))
        stride = max(1, math.ceil(len(selected) / point_count))
        points: list[int] = []
        for index in range(0, len(selected), stride):
            chunk = selected[index : index + stride]
            peak = max((abs(value) for value in chunk), default=0)
            points.append(round(peak * 1000 / 32768))
        return {
            "assetId": asset["id"],
            "available": True,
            "startSourceUs": start_us,
            "endSourceUs": end_us,
            "sampleRate": signature.sample_rate,
            "pointDurationUs": round(stride * 1_000_000 / signature.sample_rate),
            "points": points[:MAX_TIMELINE_ITEMS],
            "truncated": signature.truncated,
            "signatureDigest": key,
        }

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.rasig"

    def _read(self, key: str) -> AudioSignature | None:
        path = self._path(key)
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            if not payload.startswith(SIGNATURE_MAGIC):
                raise ValueError("signature magic")
            header_size = struct.unpack_from("<I", payload, len(SIGNATURE_MAGIC))[0]
            header_start = len(SIGNATURE_MAGIC) + 4
            header_end = header_start + header_size
            header = json.loads(payload[header_start:header_end])
            if (
                header.get("version") != SIGNATURE_VERSION
                or header.get("key") != key
                or header.get("sampleRate") != SIGNATURE_SAMPLE_RATE
            ):
                raise ValueError("signature metadata")
            samples = array("h")
            samples.frombytes(payload[header_end:])
            if os.sys.byteorder != "little":
                samples.byteswap()
            if len(samples) != int(header["sampleCount"]):
                raise ValueError("signature size")
            return AudioSignature(key, SIGNATURE_SAMPLE_RATE, samples, bool(header["truncated"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, struct.error):
            path.unlink(missing_ok=True)
            self.store.remove_cache_entry(f"audio-signature:{key}")
            return None

    def _write(self, signature: AudioSignature) -> None:
        target = self._path(signature.key)
        header = json.dumps(
            {
                "version": SIGNATURE_VERSION,
                "key": signature.key,
                "sampleRate": signature.sample_rate,
                "sampleCount": len(signature.samples),
                "truncated": signature.truncated,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        samples = array("h", signature.samples)
        if os.sys.byteorder != "little":
            samples.byteswap()
        payload = SIGNATURE_MAGIC + struct.pack("<I", len(header)) + header + samples.tobytes()
        with tempfile.NamedTemporaryFile(dir=self.root, prefix=".signature-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        os.replace(temporary, target)
        self.store.register_cache_entry(
            f"audio-signature:{signature.key}",
            "AUDIO_SIGNATURE",
            target,
            target.stat().st_size,
            prune=False,
        )

    @staticmethod
    def _extract(
        source: Path,
        duration_us: int,
        canceled: Callable[[], bool] | None,
    ) -> tuple[array[int], bool]:
        max_duration = min(
            SIGNATURE_MAX_SECONDS,
            max(1, math.ceil(duration_us / 1_000_000)) if duration_us > 0 else SIGNATURE_MAX_SECONDS,
        )
        truncated = duration_us > SIGNATURE_MAX_SECONDS * 1_000_000
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SIGNATURE_SAMPLE_RATE),
            "-t",
            str(max_duration),
            "-f",
            "s16le",
            "pipe:1",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise SignatureUnavailable("FFMPEG_UNAVAILABLE") from error
        deadline = time.monotonic() + min(180, max(20, max_duration * 2))
        while True:
            if canceled and canceled():
                _terminate_process(process)
                raise AlignmentCanceled("Alignment analysis canceled")
            try:
                stdout, _stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() < deadline:
                    continue
                _terminate_process(process, force=True)
                raise SignatureUnavailable("AUDIO_DECODE_TIMEOUT")
        if process.returncode != 0:
            raise SignatureUnavailable("AUDIO_DECODE_FAILED")
        maximum_bytes = SIGNATURE_SAMPLE_RATE * SIGNATURE_MAX_SECONDS * 2
        if len(stdout) > maximum_bytes:
            raise SignatureUnavailable("AUDIO_SIGNATURE_TOO_LARGE")
        samples = array("h")
        samples.frombytes(stdout[: len(stdout) - (len(stdout) % 2)])
        if os.sys.byteorder != "little":
            samples.byteswap()
        return samples, truncated


def _terminate_process(process: subprocess.Popen[bytes], force: bool = False) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        process.wait(timeout=1)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _next_power_of_two(value: int) -> int:
    return 1 << max(0, value - 1).bit_length()


def _fft(values: list[complex], inverse: bool = False) -> None:
    size = len(values)
    if size == 0 or size & (size - 1):
        raise ValueError("FFT length must be a non-zero power of two")
    j = 0
    for i in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            values[i], values[j] = values[j], values[i]
    length = 2
    while length <= size:
        angle = (2 if inverse else -2) * math.pi / length
        root = cmath.exp(1j * angle)
        for start in range(0, size, length):
            factor = 1 + 0j
            half = length // 2
            for offset in range(half):
                even = values[start + offset]
                odd = values[start + offset + half] * factor
                values[start + offset] = even + odd
                values[start + offset + half] = even - odd
                factor *= root
        length <<= 1
    if inverse:
        for index in range(size):
            values[index] /= size


def _convolution(left: list[float], right: list[float], *, phase_only: bool = False) -> list[float]:
    size = _next_power_of_two(len(left) + len(right) - 1)
    left_fft = [complex(value) for value in left] + [0j] * (size - len(left))
    right_fft = [complex(value) for value in right] + [0j] * (size - len(right))
    _fft(left_fft)
    _fft(right_fft)
    for index in range(size):
        product = left_fft[index] * right_fft[index]
        if phase_only:
            magnitude = abs(product)
            left_fft[index] = product / magnitude if magnitude > 1e-12 else 0j
        else:
            left_fft[index] = product
    _fft(left_fft, inverse=True)
    return [value.real for value in left_fft[: len(left) + len(right) - 1]]


def _standardized(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    if variance <= 1e-12:
        return [0.0] * len(values)
    deviation = math.sqrt(variance)
    return [(value - mean) / deviation for value in values]


def _onset_envelope(signature: AudioSignature, envelope_rate: int = 50) -> list[float]:
    stride = max(1, signature.sample_rate // envelope_rate)
    envelope = [
        sum(abs(value) for value in signature.samples[index : index + stride]) / stride
        for index in range(0, len(signature.samples), stride)
    ]
    if len(envelope) < 2:
        return envelope
    smoothed: list[float] = []
    running = 0.0
    window = 8
    history: deque[float] = deque()
    for value in envelope:
        history.append(value)
        running += value
        if len(history) > window:
            running -= history.popleft()
        baseline = running / len(history)
        smoothed.append(max(0.0, value - baseline))
    return _standardized(smoothed)


def _peak_metrics(
    values: list[float],
    lag_min: int,
    lag_max: int,
    left_length: int,
    right_length: int,
    minimum_overlap: int,
) -> tuple[int, float, float] | None:
    ranked: list[tuple[float, int]] = []
    for lag in range(lag_min, lag_max + 1):
        index = right_length - 1 - lag
        if index < 0 or index >= len(values):
            continue
        overlap = max(0, min(left_length, right_length - lag) - max(0, -lag))
        if overlap < minimum_overlap:
            continue
        ranked.append((values[index] / max(1, overlap), lag))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    best_score, best_lag = ranked[0]
    separated = [score for score, lag in ranked[1:] if abs(lag - best_lag) > 2]
    second = max(separated, default=0.0)
    ratio = best_score / max(second, 1e-9) if best_score > 0 else 0.0
    return best_lag, best_score, ratio


def correlate_audio(
    left: AudioSignature,
    right: AudioSignature,
    left_start_us: int,
    right_start_us: int,
    uncertainty_us: int = DEFAULT_UNCERTAINTY_US,
) -> CorrelationEvidence | None:
    """Return correction added to right clip placement, without mutating project state."""

    envelope_rate = 50
    left_envelope = _onset_envelope(left, envelope_rate)
    right_envelope = _onset_envelope(right, envelope_rate)
    if len(left_envelope) < envelope_rate * 2 or len(right_envelope) < envelope_rate * 2:
        return None
    correlation = _convolution(left_envelope, list(reversed(right_envelope)))
    expected_lag = round((left_start_us - right_start_us) * envelope_rate / 1_000_000)
    uncertainty = math.ceil(uncertainty_us * envelope_rate / 1_000_000)
    metrics = _peak_metrics(
        correlation,
        expected_lag - uncertainty,
        expected_lag + uncertainty,
        len(left_envelope),
        len(right_envelope),
        envelope_rate * 2,
    )
    if metrics is None:
        return None
    envelope_lag, envelope_score, envelope_ratio = metrics
    waveform_lag = round(envelope_lag * left.sample_rate / envelope_rate)
    left_index = max(0, -waveform_lag)
    right_index = max(0, waveform_lag)
    max_window = 20 * left.sample_rate
    match_length = min(
        len(left.samples) - left_index,
        len(right.samples) - right_index,
        max_window,
    )
    if match_length < 2 * left.sample_rate:
        return None
    left_window = _standardized([float(value) for value in left.samples[left_index : left_index + match_length]])
    right_window = _standardized([float(value) for value in right.samples[right_index : right_index + match_length]])
    phat = _convolution(left_window, list(reversed(right_window)), phase_only=True)
    local_uncertainty = max(1, left.sample_rate // 4)
    phat_metrics = _peak_metrics(
        phat,
        -local_uncertainty,
        local_uncertainty,
        len(left_window),
        len(right_window),
        left.sample_rate,
    )
    if phat_metrics is None:
        return None
    confirmation_lag, _phat_score, phat_ratio = phat_metrics
    observed_lag = waveform_lag + confirmation_lag
    correction_us = round(
        left_start_us - right_start_us - observed_lag * 1_000_000 / left.sample_rate
    )
    confirmation_delta_us = round(confirmation_lag * 1_000_000 / left.sample_rate)
    overlap_samples = min(len(left.samples) - left_index, len(right.samples) - right_index)
    overlap_us = round(overlap_samples * 1_000_000 / left.sample_rate)
    score_component = max(0.0, min(1.0, envelope_score / 0.35))
    uniqueness = max(0.0, min(1.0, (envelope_ratio - 1.0) / 0.35))
    phase_confirmation = max(0.0, min(1.0, (phat_ratio - 1.0) / 1.5))
    agreement = max(0.0, 1.0 - abs(confirmation_delta_us) / 250_000)
    confidence = 0.4 * score_component + 0.25 * uniqueness + 0.2 * phase_confirmation + 0.15 * agreement
    return CorrelationEvidence(
        correction_us,
        round(confidence, 6),
        round(envelope_score, 6),
        round(envelope_ratio, 6),
        round(phat_ratio, 6),
        confirmation_delta_us,
        overlap_us,
    )


def candidate_pairs(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    *,
    uncertainty_us: int = DEFAULT_UNCERTAINTY_US,
    max_per_clip: int = DEFAULT_MAX_CANDIDATES_PER_CLIP,
    max_pairs: int = DEFAULT_MAX_PAIR_COMPARISONS,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    reference_sources = {
        source["id"] for source in project.get("logicalSources", []) if source.get("reference")
    }
    ranges: list[dict[str, Any]] = []
    for clip in project.get("clips", []):
        asset = assets.get(clip["assetId"], {})
        if not _has_audio(asset) or asset.get("missing"):
            continue
        state = clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED")
        if state == "UNRESOLVED":
            continue
        duration_us = _asset_duration_us(asset)
        if duration_us <= 0:
            continue
        alignment = ClipAlignmentTransform.from_dict(clip.get("alignment") or clip.get("sync"))
        start_us = alignment.source_to_aligned(0)
        ranges.append(
            {
                "clip": clip,
                "asset": asset,
                "startUs": start_us,
                "endUs": alignment.source_to_aligned(duration_us),
                "reference": clip["logicalSourceId"] in reference_sources,
            }
        )
    ranges.sort(key=lambda item: (item["startUs"], item["clip"]["id"]))
    active: deque[dict[str, Any]] = deque()
    latest_reference: dict[str, dict[str, Any]] = {}
    counts: defaultdict[str, int] = defaultdict(int)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for current in ranges:
        while active and active[0]["endUs"] + uncertainty_us < current["startUs"]:
            expired = active.popleft()
            source_id = expired["clip"]["logicalSourceId"]
            if latest_reference.get(source_id) is expired:
                latest_reference.pop(source_id, None)
        pool = list(active)[-64:]
        pool.extend(latest_reference.values())
        ranked: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
        for other in pool:
            left_id = str(other["clip"]["id"])
            right_id = str(current["clip"]["id"])
            pair_id = tuple(sorted((left_id, right_id)))
            if pair_id in seen or other["clip"]["logicalSourceId"] == current["clip"]["logicalSourceId"]:
                continue
            if other["endUs"] + uncertainty_us < current["startUs"]:
                continue
            overlap = min(other["endUs"], current["endUs"]) - max(other["startUs"], current["startUs"])
            ranked.append(
                (
                    (
                        -int(other["reference"] or current["reference"]),
                        -max(0, overlap),
                        abs(other["startUs"] - current["startUs"]),
                        left_id,
                    ),
                    other,
                )
            )
        for _rank, other in sorted(ranked):
            left_id = str(other["clip"]["id"])
            right_id = str(current["clip"]["id"])
            if counts[left_id] >= max_per_clip or counts[right_id] >= max_per_clip:
                continue
            pair_id = tuple(sorted((left_id, right_id)))
            seen.add(pair_id)
            counts[left_id] += 1
            counts[right_id] += 1
            pairs.append((other, current))
            if len(pairs) >= max_pairs or counts[right_id] >= max_per_clip:
                break
        if len(pairs) >= max_pairs:
            break
        active.append(current)
        if current["reference"]:
            latest_reference[current["clip"]["logicalSourceId"]] = current
    return pairs


def _huber_graph_adjustments(
    clip_ids: Iterable[str],
    edges: list[dict[str, Any]],
    reference_clip_id: str,
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
    values = {clip_id: 0.0 for clip_id in clip_ids}
    support: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        support[edge["leftClipId"]].append(edge)
        support[edge["rightClipId"]].append(edge)
    for _iteration in range(12):
        next_values = dict(values)
        for clip_id in values:
            if clip_id == reference_clip_id:
                next_values[clip_id] = 0.0
                continue
            observations: list[tuple[float, float]] = [(0.0, 0.18)]
            for edge in support.get(clip_id, []):
                confidence = max(0.05, float(edge["confidence"]))
                if edge["rightClipId"] == clip_id:
                    target = values[edge["leftClipId"]] + float(edge["correctionUs"])
                else:
                    target = values[edge["rightClipId"]] - float(edge["correctionUs"])
                residual = target - values[clip_id]
                huber = 1.0 if abs(residual) <= 100_000 else 100_000 / abs(residual)
                observations.append((target, confidence * huber))
            total_weight = sum(weight for _value, weight in observations)
            next_values[clip_id] = sum(value * weight for value, weight in observations) / total_weight
        values = next_values
    return ({key: round(value) for key, value in values.items()}, dict(support))


def _reference_reachable_clip_ids(
    edges: list[dict[str, Any]], reference_clip_id: str
) -> set[str]:
    adjacency: defaultdict[str, set[str]] = defaultdict(set)
    for edge in edges:
        left_id = str(edge["leftClipId"])
        right_id = str(edge["rightClipId"])
        adjacency[left_id].add(right_id)
        adjacency[right_id].add(left_id)
    if not reference_clip_id:
        return set()
    reachable = {reference_clip_id}
    pending = [reference_clip_id]
    while pending:
        current = pending.pop()
        for neighbor in adjacency.get(current, set()):
            if neighbor not in reachable:
                reachable.add(neighbor)
                pending.append(neighbor)
    return reachable


def estimate_drift_ppm(anchors: list[tuple[int, int]]) -> int | None:
    """Estimate bounded rate only from separated, mutually consistent anchors."""

    if len(anchors) < 2:
        return None
    ordered = sorted((int(source_us), int(correction_us)) for source_us, correction_us in anchors)
    first_source, first_correction = ordered[0]
    last_source, last_correction = ordered[-1]
    separation = last_source - first_source
    if separation < 30_000_000:
        return None
    slopes = []
    for (left_source, left_correction), (right_source, right_correction) in zip(ordered, ordered[1:]):
        delta_source = right_source - left_source
        if delta_source <= 0:
            continue
        slopes.append((right_correction - left_correction) * 1_000_000 / delta_source)
    if not slopes:
        return None
    median = sorted(slopes)[len(slopes) // 2]
    if any(abs(value - median) > 250 for value in slopes):
        return None
    estimate = round((last_correction - first_correction) * 1_000_000 / separation)
    return estimate if abs(estimate) <= 2_000 else None


def analyze_project_alignment(
    project: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    signatures: AudioSignatureCache,
    *,
    canceled: Callable[[], bool] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    config = {
        "uncertaintyUs": DEFAULT_UNCERTAINTY_US,
        "maxCandidatesPerClip": DEFAULT_MAX_CANDIDATES_PER_CLIP,
        "maxPairComparisons": DEFAULT_MAX_PAIR_COMPARISONS,
        "signatureSampleRate": SIGNATURE_SAMPLE_RATE,
        "signatureMaxSeconds": SIGNATURE_MAX_SECONDS,
        "visualMatching": False,
    }
    pairs = candidate_pairs(project, assets)
    needed_asset_ids = sorted(
        {
            str(item["asset"]["id"])
            for pair in pairs
            for item in pair
        }
    )
    available: set[str] = set()
    truncated_assets: set[str] = set()
    unavailable: dict[str, str] = {}
    for index, asset_id in enumerate(needed_asset_ids):
        if canceled and canceled():
            raise AlignmentCanceled("Alignment analysis canceled")
        try:
            signature = signatures.signature(assets[asset_id], canceled)
            available.add(asset_id)
            if signature.truncated:
                truncated_assets.add(asset_id)
        except SignatureUnavailable as error:
            unavailable[asset_id] = error.reason
        if progress:
            progress(
                0.15 + 0.35 * ((index + 1) / max(1, len(needed_asset_ids))),
                f"Prepared audio evidence for {index + 1} of {len(needed_asset_ids)} clips",
            )
    signature_store = getattr(signatures, "store", None)
    if signature_store is not None:
        signature_store.prune_cache()
    loaded: OrderedDict[str, AudioSignature] = OrderedDict()

    def load_signature(asset_id: str) -> AudioSignature | None:
        if asset_id not in available:
            return None
        cached = loaded.pop(asset_id, None)
        if cached is None:
            try:
                cached = signatures.signature(assets[asset_id], canceled)
            except SignatureUnavailable as error:
                unavailable[asset_id] = error.reason
                available.discard(asset_id)
                return None
        loaded[asset_id] = cached
        while len(loaded) > 32:
            loaded.popitem(last=False)
        return cached

    edges: list[dict[str, Any]] = []
    for index, (left, right) in enumerate(pairs):
        if canceled and canceled():
            raise AlignmentCanceled("Alignment analysis canceled")
        left_signature = load_signature(str(left["asset"]["id"]))
        right_signature = load_signature(str(right["asset"]["id"]))
        if left_signature and right_signature:
            evidence = correlate_audio(
                left_signature,
                right_signature,
                int(left["startUs"]),
                int(right["startUs"]),
            )
            if evidence is not None and evidence.confidence >= 0.62:
                edges.append(
                    {
                        "leftClipId": left["clip"]["id"],
                        "rightClipId": right["clip"]["id"],
                        "correctionUs": evidence.correction_us,
                        "confidence": evidence.confidence,
                        "envelopeScore": evidence.envelope_score,
                        "envelopePeakRatio": evidence.envelope_peak_ratio,
                        "phatPeakRatio": evidence.phat_peak_ratio,
                        "confirmationDeltaUs": evidence.confirmation_delta_us,
                        "overlapUs": evidence.overlap_us,
                    }
                )
        if progress and (index + 1) % 8 == 0:
            progress(
                0.5 + 0.4 * ((index + 1) / max(1, len(pairs))),
                f"Compared {index + 1} of {len(pairs)} bounded overlap candidates",
            )
    reference_sources = {
        source["id"] for source in project.get("logicalSources", []) if source.get("reference")
    }
    reference_clip = next(
        (
            clip
            for clip in project.get("clips", [])
            if clip["logicalSourceId"] in reference_sources
            and clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED") != "UNRESOLVED"
        ),
        project.get("clips", [{}])[0],
    )
    reference_clip_id = str(reference_clip.get("id", ""))
    reachable_clip_ids = _reference_reachable_clip_ids(edges, reference_clip_id)
    anchored_edges = [
        edge
        for edge in edges
        if str(edge["leftClipId"]) in reachable_clip_ids
        and str(edge["rightClipId"]) in reachable_clip_ids
    ]
    adjustments, support = _huber_graph_adjustments(
        (str(clip["id"]) for clip in project.get("clips", [])),
        anchored_edges,
        reference_clip_id,
    )
    proposals: list[dict[str, Any]] = []
    summary = {
        "audioConfirmed": 0,
        "timestampOnly": 0,
        "conflicting": 0,
        "unresolved": 0,
        "manualAccepted": 0,
        "proposedDriftCorrections": 0,
        "candidatePairs": len(pairs),
        "confirmedEdges": len(edges),
        "signatureFailures": len(unavailable),
        "analysisCapped": len(pairs) >= DEFAULT_MAX_PAIR_COMPARISONS,
    }
    for clip in project.get("clips", []):
        asset = assets.get(clip["assetId"], {})
        current = ClipAlignmentTransform.from_dict(clip.get("alignment") or clip.get("sync"))
        current_state = clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED")
        clip_id = str(clip["id"])
        clip_edges = support.get(clip_id, [])
        accepted_edges = [edge for edge in clip_edges if float(edge["confidence"]) >= 0.72]
        corrections = []
        for edge in accepted_edges:
            correction = int(edge["correctionUs"])
            corrections.append(correction if edge["rightClipId"] == clip["id"] else -correction)
        conflict = bool(corrections and max(corrections) - min(corrections) > 250_000)
        manual = current_state == "ACCEPTED" and "manual" in clip.get("alignmentEvidence", [])
        if manual:
            classification = "MANUAL_ACCEPTED"
            confidence = float(clip.get("alignmentConfidence", 1.0))
            automatic = False
            summary["manualAccepted"] += 1
        elif conflict:
            classification = "CONFLICTING"
            confidence = max((float(edge["confidence"]) for edge in accepted_edges), default=0.0)
            automatic = False
            summary["conflicting"] += 1
        elif accepted_edges:
            classification = "AUDIO_CONFIRMED"
            confidence = min(0.99, sum(float(edge["confidence"]) for edge in accepted_edges) / len(accepted_edges))
            automatic = confidence >= 0.75
            summary["audioConfirmed"] += 1
        elif current_state != "UNRESOLVED":
            classification = "TIMESTAMP_ONLY"
            confidence = min(0.65, float(clip.get("alignmentConfidence", 0.55)))
            automatic = False
            summary["timestampOnly"] += 1
        else:
            classification = "UNRESOLVED"
            confidence = 0.0
            automatic = False
            summary["unresolved"] += 1
        adjusted = ClipAlignmentTransform(
            current.anchor_source_us,
            current.anchor_aligned_us + int(adjustments.get(clip_id, 0)),
            current.rate_ppm,
        )
        evidence_items: list[dict[str, Any]] = []
        if current_state != "UNRESOLVED":
            evidence_items.append(
                {
                    "kind": "TIMESTAMP_PRIOR",
                    "confidence": float(clip.get("alignmentConfidence", 0.55)),
                    "limitations": ["Camera clock error remains possible"],
                }
            )
        for edge in clip_edges:
            evidence_items.append(
                {
                    "kind": "AUDIO_CORRELATION",
                    "otherClipId": edge["leftClipId"] if edge["rightClipId"] == clip["id"] else edge["rightClipId"],
                    "confidence": edge["confidence"],
                    "correctionUs": edge["correctionUs"] if edge["rightClipId"] == clip["id"] else -edge["correctionUs"],
                    "envelopeScore": edge["envelopeScore"],
                    "envelopePeakRatio": edge["envelopePeakRatio"],
                    "phatPeakRatio": edge["phatPeakRatio"],
                    "confirmationDeltaUs": edge["confirmationDeltaUs"],
                    "overlapUs": edge["overlapUs"],
                }
            )
        limitations = []
        if str(asset.get("id")) in unavailable:
            limitations.append(unavailable[str(asset["id"])])
        if not clip_edges and _has_audio(asset):
            limitations.append("No bounded cross-source overlap candidate")
        if not _has_audio(asset):
            limitations.append("No usable audio stream")
        if str(asset.get("id")) in truncated_assets:
            limitations.append("Audio signature was capped at fifteen minutes")
        if any(
            edge["leftClipId"] == clip["id"] or edge["rightClipId"] == clip["id"]
            for edge in edges
        ) and clip_id not in reachable_clip_ids:
            limitations.append("Audio evidence is not connected to the reference source")
        proposals.append(
            {
                "id": opaque_id("alignment_proposal"),
                "clipId": clip["id"],
                "assetId": clip["assetId"],
                "classification": classification,
                "proposedAlignment": adjusted.to_dict(),
                "confidence": round(confidence, 6),
                "automaticallyAcceptable": automatic,
                "requiresDriftConfirmation": bool(adjusted.rate_ppm),
                "evidence": evidence_items,
                "limitations": limitations,
                "inputFingerprintDigest": digest_json(asset.get("fingerprint", {})),
            }
        )
    selection_digest = str((project.get("selectionSnapshot") or {}).get("digest", ""))
    input_digest = digest_json(
        {
            "projectId": project["id"],
            "projectRevision": project["revision"],
            "selectionDigest": selection_digest,
            "assets": [
                {
                    "assetId": clip["assetId"],
                    "fingerprint": assets.get(clip["assetId"], {}).get("fingerprint", {}),
                }
                for clip in sorted(project.get("clips", []), key=lambda item: item["id"])
            ],
            "config": config,
        }
    )
    created_at = now_iso()
    value: dict[str, Any] = {
        "id": opaque_id("alignment_set"),
        "projectId": project["id"],
        "projectRevision": project["revision"],
        "selectionDigest": selection_digest,
        "inputDigest": input_digest,
        "algorithm": "bounded-audio-evidence-graph",
        "algorithmVersion": "1",
        "config": config,
        "configDigest": digest_json(config),
        "status": "PENDING",
        "summary": summary,
        "proposals": proposals,
        "limitations": [
            "Automatic visual matching is not used",
            "Timestamp-only and unresolved clips require review",
            "Manual alignment decisions remain authoritative",
        ],
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    value["digest"] = digest_json(
        {key: item for key, item in value.items() if key not in {"id", "status", "createdAt", "updatedAt"}}
    )
    if progress:
        progress(0.95, "Solved robust alignment evidence graph")
    return value
