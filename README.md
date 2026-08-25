# Room Alignment

Room Alignment is a vendor-neutral, local-first web application for turning overlapping camera clips into one continuous program. Exactly one video source is selected for every valid output interval. Audio has its own timeline and may follow video, use a logical source, pin one exact clip, carry an explicit offset/rate transform, or be deliberate generated silence.

Every completed output is paired with a provenance manifest that identifies source assets and streams, half-open output/source ranges, synchronization decisions, user resolutions, transforms, media-tool versions, warnings, and SHA-256 digests. Source media is never modified.

## Technology and privacy boundary

- Python 3.11+ standard-library loopback service and SQLite canonical state
- dependency-free semantic HTML/CSS/JavaScript frontend
- FFprobe/FFmpeg media inspection and rendering through structured subprocess arguments
- no account, cloud service, external telemetry, remote asset, or required network request
- one-time bootstrap, server-expiring local session, CSRF, Host/Origin/fetch-metadata checks, CSP, and session-bound short-lived SSE tokens
- opaque grants for source and output directories; ordinary APIs do not accept source paths

FFmpeg and FFprobe must be available on `PATH`. Python and JavaScript package installation is not required.

## Run

```bash
python3 -m room_alignment.server
```

The service prints and opens a one-time launch URL. The default state directory is `~/.room-alignment`; use `--data-dir` to choose another location and `--no-open` to avoid opening a browser. The supported service boundary is loopback only.

## Connected workflow

1. **Library** — grant read-only access, choose full/bounded/incremental scanning, inspect warnings/evidence, select exact project assets, and set naïve-timestamp/DST policy.
2. **Align** — create/rename/merge/split/archive sources, assign clips, choose a reference, set offset or disclosed rate correction, drag/nudge tracks, inspect/correct provenance, and explicitly accept/reject suggestions.
3. **Cut** — choose one video source per interval, pin ambiguous clips, add/split/delete/reconcile blocks, and edit an independent single-source audio timeline or explicit silence.
4. **Review** — create an immutable full-hash render plan, resolve blockers, acknowledge warning codes, attest the exact plan, and render a compatible MP4 or archival FFV1/PCM Matroska artifact pair.

## Contract

The normative HTTP API is `/api/v1/openapi.json`. JSON Schemas are served under `/api/v1/contracts/`; the browser client is mechanically generated from OpenAPI. Editorial time is signed integer microseconds and all intervals are `[startUs, endUs)`.

```bash
python3 scripts/generate_api_client.py --check
```

See [API](docs/api.md), [architecture](docs/architecture.md), [data model](docs/data-model.md), [security/privacy](docs/security.md), and [runbook](docs/runbook.md).

## Verify

```bash
python3 -m unittest discover -v
python3 -m compileall -q room_alignment scripts tests
node --check web/api-client.js
node --check web/app.js
python3 scripts/benchmark_local.py
```

Read-only corpus validation accepts paths only as runtime arguments so private paths and names are never committed:

```bash
python3 scripts/validate_reference_corpus.py /path/to/corpus --state-dir /separate/state/directory
```

Canonical-state administration:

```bash
python3 scripts/state_admin.py verify /path/to/room-alignment.sqlite3
python3 scripts/state_admin.py backup /path/to/room-alignment.sqlite3 /separate/backup.sqlite3
python3 scripts/state_admin.py dry-run-migrate /separate/backup.sqlite3
python3 scripts/state_admin.py restore /path/to/room-alignment.sqlite3 /separate/backup.sqlite3 --replace
```

Restore refuses to run while the application owns the state directory and creates a verified rollback copy before replacement. Source media is never included in application-state backup.

## Delivery state

This repository is a local source release candidate only when the requirement ledger and verification report say so. Packaging, installation, signing, notarization, publication, deployment, and distribution are separate authority states.
