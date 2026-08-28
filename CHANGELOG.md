# Changelog

## 0.1.0 — First public release

### Added

- PEP 517 wheel and source-archive build with backend, frontend, contracts, license, and notices
- `room-alignment` launcher with serve, doctor, and state-administration commands
- Clean-wheel installation/runtime verifier with authenticated resource and graceful-shutdown checks
- Vendor-agnostic local video indexing and evidence-based provenance
- Persistent SQLite libraries, projects, and render jobs
- Library → Align → Cut → Review desktop workflow
- Independent Program Video and Program Audio decisions
- Hardware-accelerated VideoToolbox output presets for reusable program cuts
- Provenance manifest, preflight, cancellation, and atomic output completion

### Changed

- Runtime resource lookup now uses wheel-contained package data with source-checkout fallback
- API, health, render-plan, package metadata, and command output share version `0.1.0`
- Library selection presents timestamp-overlap event windows instead of whole-day clip batches
- Project creation requires explicit confirmation of proposed logical sources and can group repeated clips into one source
- Align plays authenticated read-only source previews on a shared evidence clock
- Timestamp proposals can be previewed and accepted as one atomic, reversible project command

### Fixed

- Source-candidate fingerprints no longer include per-file evidence origins that made every clip appear to be a different camera
- Timestamp-anchored disconnected components can become eligible without false global-reference conflicts
- Explicit downtime exclusion allows long evidence sessions to produce covered output
- Optimized overlapping slices pin exact clips before rendering
- Complex filter graphs use bounded concurrency to avoid resource exhaustion

### Known limitations

- FFmpeg and FFprobe remain external Homebrew dependencies and are not redistributed
- Package and release artifacts are not signed or notarized
- Browser UI is local-first and does not include a packaged native desktop shell
