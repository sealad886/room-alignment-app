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

FFmpeg and FFprobe 6.0 or newer must be available on `PATH`. They remain separate programs because FFmpeg redistribution terms depend on build configuration. Room Alignment itself ships as one Python wheel containing backend, frontend, contracts, license material, and command-line launcher.

## Install

```bash
uv tool install /path/to/room_alignment-0.1.0-py3-none-any.whl
room-alignment doctor
```

`pipx install` or installation into a dedicated virtual environment is also supported. Do not install into system Python.

Start the application:

```bash
room-alignment
```

The command starts the loopback service and opens a one-time secure browser URL. The default state directory is `~/.room-alignment`; use `room-alignment serve --data-dir PATH`, `--port`, and `--no-open` for explicit operation. The legacy `room-alignment --no-open ...` option shape remains supported. `python -m room_alignment` launches the same installed entry point.

In-app Settings persist in the same local database as project state. They control a symmetric 0–300 second overlap-analysis search extension, four text sizes, and four color schemes. The 30-second default helps compensate for capture-to-hub timestamp skew while existing per-clip and whole-job comparison caps keep analysis bounded.

Stop the process owning the default or an explicit state directory:

```bash
room-alignment stop
room-alignment stop --data-dir PATH
```

Shutdown is graceful by default. `--timeout SECONDS --force` permits a forced stop only after the validated state-directory owner fails to release its lock.

Administrative state operations use the same package:

```bash
room-alignment admin verify STATE.sqlite3
room-alignment admin backup STATE.sqlite3 BACKUP.sqlite3
room-alignment admin dry-run-migrate BACKUP.sqlite3
room-alignment admin restore STATE.sqlite3 BACKUP.sqlite3 --replace
```

## Build and verify package

Build tooling is isolated and build-only:

```bash
uv build
python3 scripts/verify_package.py dist/room_alignment-0.1.0-py3-none-any.whl
```

Verification installs the wheel into a fresh temporary virtual environment with `--no-index --no-deps`, launches it from outside the repository, bootstraps a session, loads frontend and OpenAPI resources, checks health/version, sends SIGTERM, and relaunches against the same state directory to prove lock recovery. It does not scan source media or publish artifacts.

Source development remains available through `python3 -m room_alignment` from a checkout.

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

GitHub Actions runs these source checks on branch pushes and pull requests,
using both Python 3.11 and 3.13. It then builds and clean-installs the wheel on
macOS, retains the wheel and source archive for 14 days, and applies the same
package runtime verification described above. A `v*` tag must exactly match
the package/runtime version and an existing changelog release section. Release
CI rebuilds and verifies the package on macOS, generates SHA-256 checksums, and
publishes the wheel and source archive to the matching GitHub release.

Read-only corpus validation accepts paths only as runtime arguments so private paths and names are never committed:

```bash
python3 scripts/validate_reference_corpus.py /path/to/corpus --state-dir /separate/state/directory
```

Restore refuses to run while the application owns the state directory and creates a verified rollback copy before replacement. Source media is never included in application-state backup.

## License

Room Alignment is licensed under the [Apache License 2.0](LICENSE).
Copyright 2026 Andrew M. Cox.

FFmpeg and FFprobe are separate, user-supplied programs and are not distributed
as part of Room Alignment. Their own license terms apply.

## Project status

Current implementation provides an installable wheel/source archive, cohesive launcher and administration CLI, real scanning, alignment/edit-decision persistence, preflight, manifest generation, FFmpeg rendering, render cancellation, and connected browser UI. See [runbook](docs/runbook.md), [architecture](docs/architecture.md), and [risk register](docs/risk-register.md).

## Delivery state

This repository can produce a locally verified installable package. Signing, notarization, publication, deployment, distribution, and installation outside isolated verification remain separate authority states.
