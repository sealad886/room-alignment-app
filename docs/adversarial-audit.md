# Adversarial implementation audit

Audit date: 2026-08-26

## Scope and done criteria

Target: complete local service, installable distribution, canonical domain/compiler, persistence/migration, scan/provenance, render/recovery pipeline, generated API client, and connected Library → Align → Cut → Review UI.

Failure meant any silent provenance/timing error, source/output mutation, cross-library authority confusion, stale UI state, unbounded representative workload, insecure local boundary, mixed/partial migration, false artifact success, inaccessible connected flow, or unsupported delivery claim. Closure required a reproducer or source-path proof, a minimal correction, focused regression evidence, a full-suite rerun, and a current disposition ledger.

## Evidence index

- Contracts: `contracts/*.json`, `contracts/events.md`, `tests/test_contracts.py`
- Timing/compiler/commands: `room_alignment/domain.py`, `tests/test_domain.py`
- Grants/scans/state/migrations/events: `room_alignment/store.py`, `room_alignment/scanner.py`, store/scanner/state tests
- Render/plans/manifests/recovery: `room_alignment/render.py`, render/media-matrix tests
- Local boundary: `room_alignment/server.py`, `tests/test_server_v1.py`
- Connected UI: `web/`, generated-client check, Chromium flow/contrast/responsive audit
- Scale/corpus: `scripts/benchmark_local.py`, `scripts/validate_reference_corpus.py`, sanitized results in `verification-report.md`

## Closed findings

| ID | Severity | Finding and root cause | Remediation | Proof | Status |
|---|---|---|---|---|---|
| AUD-001 | High | Project creation could combine an asset ID from another library because membership was not validated after lookup. | Validate active project library and require every selected record’s library ID to match. | `test_project_rejects_media_from_a_different_library` | Fixed |
| AUD-002 | High | Grant revocation only found some source-library jobs and did not ensure queued/output render termination. | Discover jobs through library, project, and artifact/output grant; persist `CANCEL_REQUESTED` with `GRANT_REQUIRED`; poll/terminate scan, analysis, and render workers; finish failed without output. | revocation store test and `test_output_grant_revocation_stops_queued_render_as_failed` | Fixed |
| AUD-003 | High | Frontend could miss a terminal job event between first poll and SSE listener registration, leaving Review visually queued. | Keep low-frequency canonical polling active even when SSE is healthy; SSE remains latency path and reset/replay remains recovery path. | connected two-slice render reached visible `SUCCEEDED`; current browser run has no errors | Fixed |
| AUD-004 | Medium | Injected staged-migration failure removed the main staging file before SQLite closed, leaving `-wal`/`-shm` remnants. | Close staging/source connections first, remove exact staging/WAL/SHM files, retain verified backup and original DB. | `test_failed_staged_migration_keeps_original_and_removes_staging_file` | Fixed |
| AUD-005 | Medium | Shutdown released the state lock without a bounded worker join; a slow worker could race the next owner. | Cancel scan/analysis/render work, bounded-join workers before unlock, kill unsettled render process groups, persist interruption/recoverable state. | full suite, restart/job tests, code-path audit | Fixed |
| AUD-006 | Medium | A failure after one artifact promotion could remain a generic failed artifact until restart. | Mark any exception with one final member present as `FAILED_RECOVERABLE`; startup still quarantines/reconciles exact partials and never reports complete. | render recovery tests and artifact-state inspection | Fixed |
| AUD-007 | Medium | Workflow step numbers used a 3.68:1 color and failed normal-text WCAG AA. | Use the established muted token, measured at 7.16:1 on the topbar. | Chromium audit: 0 contrast failures | Fixed |
| AUD-008 | Low | An SSE-connected UI stopped fallback polling, and a disconnected browser could cause a server traceback. | Continuous safety polling plus safe HTTP connection-error handling without traceback/path disclosure. | Chromium: 0 errors; full loopback tests | Fixed |
| AUD-009 | Low | Runtime/package version and delivery/risk documents had stale pre-implementation claims. | Align runtime/API/manifest/package version and replace stale delivery, risk, audit, and verification claims with measured evidence. | version/source search and documentation review | Fixed |
| AUD-010 | High | Render, analysis, and synchronous full-hash plan creation were not all explicitly backpressured, permitting local resource amplification. | Reserve one render, permit two analyses, serialize full-hash plan creation, and return stable `JOB_STATE_CONFLICT` for excess work. | render/analysis backpressure tests and full suite | Fixed |
| AUD-011 | High | Console entry referenced source-tree-only `web/` and `contracts/`, while `pyproject.toml` had no build backend or package-data rules, so an installed wheel could not serve the product. | Add pinned PEP 517 build, force-include resources inside package, resolve installed data first, and verify wheel from outside repository. | two byte-identical builds plus clean-wheel frontend/API smoke | Fixed |
| AUD-012 | High | First package doctor draft treated any FFmpeg/FFprobe version as supported despite documented 6.0 floor. | Parse bounded first-line major version and fail readiness when absent or below 6. | `test_doctor_rejects_media_tool_below_supported_floor` and installed doctor result | Fixed |

No critical finding was reproduced. No accepted in-scope finding remains open or blocked.

## Bounded residual risks

- FFmpeg/FFprobe remain a complex native parser surface; Python wheel packaging does not add an OS sandbox. Bounds, current maintained tools, structured argv, local grants, and process ownership reduce but cannot eliminate that risk.
- Same-OS-user malware may already have wider filesystem/process access than browser-origin controls can prevent.
- Media compatibility evidence is representative, not exhaustive across every codec/container/build/hardware combination. Unsupported required transforms block rather than approximate.
- Automatic timestamp/cluster suggestions are deliberately low-authority assistance; manual verification remains necessary.

These residuals match the product’s accepted local-single-user boundary and do not weaken source immutability, explicit uncertainty, single-view compilation, independent audio, or artifact truth.
