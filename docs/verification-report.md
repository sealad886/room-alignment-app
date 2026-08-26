# Installable package verification report

Date: 2026-08-26 (Europe/Dublin)

Scope: version 0.3.0 package candidate in isolated `codex/installable-package` worktree, based on merged PR #2 commit `c27bca3`. Evidence covers local package build and temporary isolated installation. It does not claim signing, notarization, publication, distribution, system-wide installation, deployment, or production operation.

## Unreleased alignment usability correction on `main`

On 2026-08-26, the live loopback app was restarted from the current `main` working tree against its existing local state and inspected in the user-selected Brave browser with the indexed reference corpus. Evidence for this unreleased correction is separate from the 0.3.0 package artifact evidence below.

- `python -m unittest discover -s tests` — **115 tests passed**, including explicit source-partition validation, atomic multi-suggestion application, canonical suggestion-tampering rejection, stable source candidates across per-file evidence origins, authenticated source-preview range reads, and source-byte preservation.
- `python scripts/generate_api_client.py --check`, `python -m compileall -q room_alignment scripts tests`, `node --check web/app.js`, `node --check web/api-client.js`, and `git diff --check` passed.
- Brave displayed loaded-media event windows: the first observed window contained 10 clips and 3 proposed logical sources instead of a whole-day 500-clip project.
- Project creation remained disabled until the proposed source grouping was explicitly confirmed.
- Six real source videos loaded through authenticated `/api/v1/media/{mediaId}/preview` requests with no media errors. Seeking the shared playhead to 50% placed all six previews at 30.550 seconds; only the selected source was audible.
- The final Brave trace showed successful preview metadata probes (`HEAD 200`) and byte-range playback (`GET 206`) with no preview 5xx responses; disconnected range clients are handled without false server errors.
- Existing legacy projects are not rewritten automatically. The previously created 500-source project remains available exactly as saved; creating a new project through the corrected Library flow applies the explicit grouped-source contract.
- A wheel and source archive were built into a temporary directory after the final integrity fix. Clean-wheel verification reported frontend loaded, health/OpenAPI/state administration ready, clean SIGTERM, reusable state lock, checkout-independent resources, and wheel SHA-256 `3b2b472ba92f1912bdb73dab35e5a0bf7e1167cc505efbcc2c0110399902f4b2`. They were not installed system-wide or published.
- Pinned Ruff 0.16.3 reports 52 non-blocking findings and pinned Pyright 1.1.411 reports the same 21 baseline type findings. Per the P2 policy these remain deferred under issue #7; this milestone did not auto-fix or accept them.

## Automated and contract verification

- `PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -v` — **109 tests passed**, 0 failed, 0 skipped; includes package CLI/resource/port-lock cases, loopback security/session tests, and native FFmpeg renders.
- `python3 scripts/generate_api_client.py --check` — generated browser client matches normative OpenAPI.
- `python3 -m compileall -q room_alignment scripts tests` — passed.
- `node --check web/api-client.js` and `node --check web/app.js` — passed.
- `git diff --check` — passed.
- Contract tests parse every normative JSON schema, enumerate required route families/error codes/commands, and verify generated-client currency.

## Package build and clean-install evidence

- `uv build --clear` produced `room_alignment-0.3.0-py3-none-any.whl` and `room_alignment-0.3.0.tar.gz` through pinned Hatchling 1.27.0 in an isolated PEP 517 environment.
- Repeated independent builds were byte-for-byte identical for both wheel and source archive.
- Wheel SHA-256: `0b91084db5a064703dcb702ca48553978ef8940e958621e32b1b828fc9ee574e`.
- `scripts/verify_package.py` inspected required wheel members and license/entry-point metadata, created a fresh temporary virtual environment, installed with `--no-index --no-deps`, and launched from outside the repository.
- Installed runtime loaded frontend, health, authenticated system/OpenAPI resources, and reported version 0.3.0 through both console and `python -m room_alignment` entry points. Installed state-administration command returned `integrity=ok`. `room-alignment doctor` confirmed packaged resources plus FFmpeg/FFprobe 9.0.1 against minimum major 6.
- SIGTERM exited cleanly and immediate relaunch against the same state directory succeeded, proving application-lock release. Port-bind failure has a separate lock-release regression test.
- Verifier never granted or scanned a media directory; no source media or private path entered either artifact.

Pinned Ruff 0.16.3 reports 51 non-blocking style/modernization findings and pinned Pyright 1.1.411 reports 21 basic-mode findings, predominantly inherited from the merged baseline. No behavioral failure was reproduced; per P2 policy they are deferred to [issue #7](https://github.com/sealad886/room-alignment-app/issues/7), not silently accepted or expanded into this package milestone.

## Connected browser and accessibility evidence

A fresh Chromium run completed Library scan → exact media selection → project creation → Align → Cut → Review against two synthetic videos through the real loopback API. The current revision produced an immutable render plan. A prior connected run also completed a two-video/two-audio-slice render and loaded the completed video/manifest artifact pair.

Post-hardening audit at 1280 × 900 and 800 × 900 reported:

- 11 visible Review controls, 0 unnamed controls;
- 0 WCAG text-contrast failures;
- document width exactly 800 px at the 800 px viewport, with no horizontal overflow;
- Review controls remained visible;
- 0 browser console/page errors.

Manual keyboard/runtime coverage included source selection, numeric and keyboard alignment nudge, program video cut, independent fixed-source audio, Review, durable progress, and completed-artifact retrieval. Visual hierarchy remains matched to the accepted Open Design fidelity ledger.

## Performance evidence

`python3 scripts/benchmark_local.py` used a synthetic 26,520-asset index and approximately 1,000 program blocks:

- ordinary local read p95: 13.536 ms (target < 200 ms);
- project command p95: 27.961 ms (target < 100 ms);
- event replay: 0.969 ms (target < 2,000 ms);
- synthetic indexing: 3.905 seconds;
- peak tracked Python allocation: 631,308 bytes, demonstrating bounded streaming behavior rather than whole-library record retention.

## Representative read-only corpus evidence

The runtime-only corpus validator completed a full read-only scan with four probe workers:

- source files observed: 27,381;
- video assets indexed: 26,520;
- source bytes observed: 213,547,261,865;
- first durable progress: 0.05 seconds (target < 2 seconds);
- elapsed scan: 339.974 seconds;
- peak tracked Python allocation: 1,756,445 bytes;
- warnings: 0;
- source tree metadata preserved: **true**, proven by identical sanitized path/size/mtime SHA-256 tree digest before and after. This check does not read and hash the full contents of every source file.

The runtime path and identifying filenames are intentionally absent from this repository and report.

## Media, migration, security, and recovery evidence

- FFmpeg/FFprobe integration rendered compatible H.264/AAC MP4 and exercised FFV1/PCM planning, mixed raster/frame/audio normalization, independent audio, manifest parity, source mutation, collision, cancellation, and partial recovery cases.
- Migration tests prove deterministic half-even time conversion, explicit-silence preservation, unknown legacy-field recovery, invalidated legacy review, verified backup/restore, and original-state survival plus staging cleanup after injected migration failure.
- Security tests cover bootstrap reuse/redaction, session expiry, Host/Origin/fetch-metadata/CSRF rejection, session-bound event tokens, symlink escape, bounded inputs, structured subprocesses, grant overlap/revocation, and path-safe errors/logs.

## Delivery-state boundary

Core implementation and PR #2 merge are complete. Local package build plus temporary clean installation/runtime verification are complete. Artifact files remain local and ignored by Git. No push/PR for package changes, signing, notarization, publication, deployment, distribution, system-wide installation, or production observation was authorized or performed.
