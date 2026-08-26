# Local release-candidate verification report

Date: 2026-08-25 (Europe/Dublin)

Scope: local source release candidate in the isolated `codex/technology-agnostic-backend` worktree. Evidence does not claim packaging, installation, publication, deployment, or production operation.

## Automated and contract verification

- `PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -v` — **56 tests passed**, 0 failed, 0 skipped outside the sandbox; includes loopback security/session tests and native FFmpeg renders.
- `python3 scripts/generate_api_client.py --check` — generated browser client matches normative OpenAPI.
- `python3 -m compileall -q room_alignment scripts tests` — passed.
- `node --check web/api-client.js` and `node --check web/app.js` — passed.
- `git diff --check` — passed.
- Contract tests parse every normative JSON schema, enumerate required route families/error codes/commands, and verify generated-client currency.

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

Implementation, local verification, browser observation, and representative-corpus observation are complete. The branch is pushed to `origin` and pull request #2 is open for automated review. Packaging, installation, merge, signing, notarization, publication, deployment, distribution, and production observation were not authorized or performed.
