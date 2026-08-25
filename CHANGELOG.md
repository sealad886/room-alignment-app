# Changelog

## 0.1.0 — Local development baseline

### Added

- Vendor-agnostic local video indexing and evidence-based provenance
- Persistent SQLite libraries, projects, and render jobs
- Library → Align → Cut → Review desktop workflow
- Independent Program Video and Program Audio decisions
- Wall-clock and source-clip cut anchoring with explicit reconciliation
- Provenance manifest, preflight, H.264/AAC and FFV1/PCM rendering
- Read-only source safety, same-origin loopback checks, cancellation, and atomic output

### Known limitations

- No packaged desktop shell, signing, or notarization
- Manual alignment only
- Scan fingerprints do not yet skip unchanged FFprobe work
- No waveform/frame thumbnail cache
- Broader codec/geometry corpus qualification remains recommended before public distribution

