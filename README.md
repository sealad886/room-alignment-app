# Room Alignment

Room Alignment is a local-first web application for synchronizing video clips from overlapping cameras, choosing exactly one video source for each output interval, controlling audio independently, and rendering a provenance-preserving result.

It is vendor-agnostic. Filenames, folder dates, MP4/MOV tags, JSON sidecars, importer hints, and user corrections are evidence sources—not required naming contracts. Unknown fields are retained.

## Safety model

- Source libraries are opened read-only.
- Original media is never renamed, moved, rewritten, or timestamp-adjusted.
- SQLite state, editorial decisions, and manifests live outside source library.
- Render output uses a temporary sibling and atomic promotion.
- Missing coverage, output gaps, overlaps, and incomplete provenance block rendering.
- Local HTTP service binds to `127.0.0.1` by default.

## Requirements

- macOS or another Unix-like system with Python 3.11+
- FFmpeg and FFprobe on `PATH`
- Modern browser

No Python or JavaScript packages are required.

## Run

```bash
python3 -m room_alignment.server
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Application data defaults to `~/.room-alignment`.

Use another port or data directory:

```bash
python3 -m room_alignment.server --port 8877 --data-dir /path/to/app-state
```

## Workflow

1. **Library** — index any local folder of supported video files. Choose bounded initial scan or entire library.
2. **Align** — select reference source and set reversible synchronization offsets.
3. **Cut** — select Program Video per interval, link/unlink Program Audio, and reconcile explicit gaps/overlaps.
4. **Review** — inspect preflight, fidelity plan, and manifest; mark reviewed; render MP4 or lossless intermediate.

Supported discovery extensions: MP4, MOV, MKV, M4V, AVI, WebM, MTS, M2TS, and TS. Unsupported or malformed files become warnings rather than aborting whole scan.

## Test

```bash
python3 -m unittest discover -v
python3 -m compileall -q room_alignment tests
node --check web/app.js
```

## License

Room Alignment is licensed under the [Apache License 2.0](LICENSE).
Copyright 2026 Andrew M. Cox.

FFmpeg and FFprobe are separate, user-supplied programs and are not distributed
as part of Room Alignment. Their own license terms apply.

## Project status

Current implementation provides real scanning, alignment/edit-decision persistence, preflight, manifest generation, FFmpeg rendering, render cancellation, and connected browser UI. See [runbook](docs/runbook.md), [architecture](docs/architecture.md), and [risk register](docs/risk-register.md).
