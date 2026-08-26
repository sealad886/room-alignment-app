# Changelog

## Unreleased

### Changed

- Library selection now presents timestamp-overlap event windows instead of whole-day clip batches
- Project creation requires explicit confirmation of proposed logical sources and can group repeated clips into one source
- Align now plays up to six authenticated read-only source previews on the shared output clock
- Pending timestamp suggestions can be previewed and accepted as one atomic, reversible project command

### Fixed

- Source-candidate fingerprints no longer include per-file evidence origins that made every clip appear to be a different camera

## 0.3.0 — Installable local application package

### Added

- PEP 517 wheel and source-archive build with backend, frontend, contracts, license, and notices
- `room-alignment` launcher with serve, doctor, and state-administration commands
- Clean-wheel installation/runtime verifier with authenticated resource and graceful-shutdown checks

### Changed

- Runtime resource lookup now uses wheel-contained package data with source-checkout fallback
- API, health, render-plan, package metadata, and command output share version `0.3.0`

### Distribution boundary

- FFmpeg and FFprobe remain external dependencies and are not redistributed
- Package is not signed, notarized, published, or installed system-wide by the build workflow

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
