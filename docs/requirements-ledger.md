# Requirement disposition ledger

`Implemented` means the behavior exists in the referenced source; it does not by itself mean every release gate has run. `Verified` adds automated/runtime evidence. The verification report records current command output and corpus/browser results. No requirement is silently waived.

| Requirement | Disposition | Implementation and evidence |
|---|---|---|
| REQ-001 | Implemented | Vendor-neutral domain/schema/labels; source candidates are advisory and never silently merged (`domain.py`, `test_domain.py`). |
| REQ-002 | Implemented | Scanner/probe/render only open source files for reading; corpus validator compares before/after tree digest. |
| REQ-003 | Implemented | Grant overlap checks and output/cache/state separation (`store.py`, grant tests). |
| REQ-004 | Implemented | Loopback-only dependency-free core; no telemetry/remote assets (`server.py`, `security.md`). |
| REQ-005 | Implemented | Named revisioned commands preserve decisions; raw evidence immutable. |
| REQ-006 | Verified | Conflicts/absence/uncertainty preserved and tested (`provenance.py`, `test_provenance.py`). |
| REQ-007 | Verified | Compiler emits video/audio gaps; only explicit `SILENCE` synthesizes audio (`test_domain.py`). |
| REQ-008 | Verified | Compiler and schemas use `[startUs,endUs)`; interval properties tested. |
| REQ-009 | Implemented | Backend opaque IDs across domain/job/plan/artifact; labels/paths are not identity. |
| REQ-010 | Implemented | Responsive keyboard-operable local web UI and wrapper-compatible API. |
| REQ-011 | Implemented | Explicit grant form is the sole path-accepting ordinary API; later calls use grant IDs. |
| REQ-012 | Verified | Read/write roles and cross-role overlap rejection (`test_store_v1.py`). |
| REQ-013 | Verified | Resolve-under-root on scan/render/retrieval; symlink escape test. |
| REQ-014 | Verified | Revocation blocks new access and interrupts dependent jobs without deleting decisions. |
| REQ-015 | Implemented | Incremental/full/bounded scan modes exposed in API; full/bounded connected UI. |
| REQ-016 | Verified | Durable scan IDs/generations bind progress/results/pagination. |
| REQ-017 | Verified | Only successful full scan marks unobserved assets missing. |
| REQ-018 | Verified | Identity-key rename continuity only when unambiguous; otherwise separate. |
| REQ-019 | Verified | Media/container/path/sidecar fingerprint changes invalidate cached probe/suggestions/plans. |
| REQ-020 | Verified | Streaming walk, 50-record persistence batches, bounded worker queue; scale benchmark records peak allocation. |
| REQ-021 | Verified | Malformed/unreadable asset becomes warning; scan retains partial progress on failure. |
| REQ-022 | Implemented | Idempotent cancel, prompt scheduling stop, owned ffprobe process-group termination. |
| REQ-023 | Verified | Cursor encodes generation/path/ID; concurrent generation test prevents reordering/duplication. |
| REQ-024 | Verified | Known containers plus signature-admitted unknown extensions; actual ffprobe status/reason. |
| REQ-025 | Implemented | Corpus path is runtime-only; validator emits sanitized aggregates/digest and writes separate state. |
| REQ-026 | Verified | Evidence records field/raw/normalized/kind/origin/time/extractor/version/confidence/uncertainty/custom. |
| REQ-027 | Implemented | Runtime/schema permit filesystem/filename/container/sidecar/importer/user and future string kinds. |
| REQ-028 | Verified | Evidence merge retains conflicts; resolution is separate ledger. |
| REQ-029 | Verified | Revisioned correction records previous/new/rationale/actor/time. |
| REQ-030 | Verified | Namespaced custom sidecar fields retained internally; manifest exports only selected resolution/provenance fields. |
| REQ-031 | Verified | Distinct `MediaAsset`, `LogicalSource`, `ProjectClip`; same-candidate separation test. |
| REQ-032 | Verified | `SourceCandidate` evidence remains `USER_REVIEW_REQUIRED`; never final identity automatically. |
| REQ-033 | Implemented | Connected create/rename/merge/split/archive/reassign controls use commands. |
| REQ-034 | Implemented | Library checkboxes select exact project assets; clusters remain suggestions. |
| REQ-035 | Verified | Candidate/source IDs, not labels, drive source distinction and coverage. |
| REQ-036 | Verified | Manifest uses stable IDs, relative paths, SHA-256; excludes roots/unrelated custom fields. |
| REQ-037 | Verified | Canonical public/editorial time uses integer `*Us`; schema/property tests. |
| REQ-038 | Verified | Native stream metadata, source range, and output ranges are separate fields/conversions. |
| REQ-039 | Verified | Stream timebase/start PTS/duration/VFR/edit/color/rotation evidence retained. |
| REQ-040 | Verified | Missing absolute timestamp does not prevent project use/manual alignment. |
| REQ-041 | Verified | Timestamp normalization retains raw/timezone/UTC/ambiguity/fold/nonexistent/confidence. |
| REQ-042 | Verified | Library IANA/DST policy API/UI re-normalizes and invalidates suggestions, preserving raw evidence. |
| REQ-043 | Verified | Half-even microsecond conversion documented/tested; migration retains originals/version. |
| REQ-044 | Verified | Per-clip anchor/offset/rate affine transform implements fixed equation. |
| REQ-045 | Verified | Non-zero drift requires confirmation, appears in UI/plan/manifest, affects fidelity. |
| REQ-046 | Verified | ±2,000 ppm documented bound; out-of-range rejected, never clamped. |
| REQ-047 | Verified | Monotonic deterministic inverse round-trip property within one microsecond. |
| REQ-048 | Verified | Rescans/suggestions do not overwrite manual sync or timestamp resolutions. |
| REQ-049 | Implemented | Reference command preserves mappings; previews return affected ranges/issues. |
| REQ-050 | Implemented | Numeric offset/rate, keyboard nudges, playhead, selection, pointer track movement. |
| REQ-051 | Implemented | Align contains canonical Program Video and Program Audio lanes. |
| REQ-052 | Verified | Alignment analysis is durable asynchronous job; editing remains available. |
| REQ-053 | Verified | Suggestions persist algorithm/version/config/input digest/revision/transform/confidence/evidence/limits. |
| REQ-054 | Verified | Pending/accepted/rejected/stale/superseded contract; accept/reject named commands/UI. |
| REQ-055 | Verified | Asset/source/provenance/time-policy/project/algorithm inputs stale dependent suggestions. |
| REQ-056 | Implemented | UI displays evidence, limitations, incomplete/manual authority; fixture tests cover fields. |
| REQ-057 | Implemented | Timestamp method is optional assistive algorithm; manual alignment complete without it. |
| REQ-058 | Verified | Preview command returns exact affected intervals and introduced issues before commit. |
| REQ-059 | Verified | Video block selects logical source with optional exact clip pin. |
| REQ-060 | Verified | One logical block compiles across consecutive clips into exact source slices. |
| REQ-061 | Verified | Single-view invariant/coverage compiler and render preflight enforce exactly one video. |
| REQ-062 | Verified | Complete/gap/ambiguous/unavailable/sync-unresolved classifications represented by canonical issue codes. |
| REQ-063 | Verified | Ambiguous coverage blocks until exact clip pin; connected pin control. |
| REQ-064 | Implemented | Initialize/assign/pin/split/move/delete/add/reconcile video commands and UI controls. |
| REQ-065 | Verified | Deterministic disclosed initialization prefers reference and leaves uncovered/ambiguous issues. |
| REQ-066 | Verified | Independent audio block array/count/boundaries; property/scenario tests. |
| REQ-067 | Verified | FOLLOW_VIDEO/FIXED_SOURCE/FIXED_CLIP/SILENCE schema/compiler/UI. |
| REQ-068 | Verified | Follow-video without usable audio raises issue; no implicit silence. |
| REQ-069 | Verified | Fixed logical source compiles across clips with ambiguity/availability checks. |
| REQ-070 | Verified | Fixed clip pins stream/range and provenance independently. |
| REQ-071 | Verified | Synthetic silence only through explicit block, disclosed in slice/manifest. |
| REQ-072 | Verified | Independent audio offset/rate with bounded confirmation and manifest transform. |
| REQ-073 | Implemented | Compiler accepts one selection/silence only; no mixing/gain/ducking/spatial controls. |
| REQ-074 | Verified | Program-clock mode keeps output cuts while source ranges shift. |
| REQ-075 | Verified | Source-attached mode preserves source cut points and exposes moved gaps/overlaps. |
| REQ-076 | Implemented | Anchoring/sync preview and explicit confirmation when issues/boundaries change. |
| REQ-077 | Implemented | Gap/overlap/range reconciliation are scoped named commands with preview. |
| REQ-078 | Verified | Backend compiler owns issues; frontend reloads canonical compiled state after command. |
| REQ-079 | Verified | `program-at` returns canonical video/audio stream/range/source/transform/provenance/issues. |
| REQ-080 | Verified | Immutable plan binds exact revision/compiled program/settings/provenance/fingerprints. |
| REQ-081 | Verified | Deterministic plan digest excludes creation time and covers all output-affecting inputs. |
| REQ-082 | Verified | Full SHA-256 selected-source digest required before render-authorizing review. |
| REQ-083 | Verified | Size/full digest revalidated before and after media process; mutation blocks completion. |
| REQ-084 | Verified | Preflight blocker codes cover canonical issues, sources, review, destination, disk, transforms. |
| REQ-085 | Verified | Stable warning codes separate from blockers; attestation records acknowledged codes. |
| REQ-086 | Verified | Attestation binds project/revision/plan digest/source set/provenance/warnings/time. |
| REQ-087 | Verified | Project/source/provenance/timing/settings/destination/warning changes stale review. |
| REQ-088 | Verified | Compatible H.264/AAC MP4 and archival FFV1/PCM Matroska profiles declared in plan. |
| REQ-089 | Implemented | Stream copy is intentionally unavailable in v1 rather than approximated; plan says false. |
| REQ-090 | Verified | Plan declares raster/aspect/rotation/SAR/frame/color/HDR/pixel/audio/codec policies. |
| REQ-091 | Verified | HDR source blocks absent explicit conversion; color/rotation warnings are disclosed. |
| REQ-092 | Verified | Render integration compares decoded duration/A-V timing within frame/sample tolerances. |
| REQ-093 | Verified | Estimated+margin preflight and continuing free-space floor during render. |
| REQ-094 | Verified | Existing video/manifest collision blocks; no overwrite route. |
| REQ-095 | Verified | Unique partial video/manifest, atomic per-file promotion, COMPLETE only after both+digests. |
| REQ-096 | Verified | Startup reconciles one/both/partial states to recoverable, never false success. |
| REQ-097 | Verified | Manifest schema records identities/ranges/transforms/resolutions/tools/digests/warnings/fidelity. |
| REQ-098 | Verified | Fidelity distinguishes re-encode/lossless-after-processing/resample/scale/rate/silence; never calls source untouched. |
| REQ-099 | Implemented | Artifact/status/video/manifest routes and retained immutable project revision retrieval. |
| REQ-100 | Verified | Idempotent cancellation owns process group/exact partials and preserves pre-existing finals. |
| REQ-101 | Verified | All app APIs under `/api/v1`; served OpenAPI is normative. |
| REQ-102 | Verified | Browser client mechanically generated from OpenAPI operation IDs; stale check fails build. |
| REQ-103 | Verified | No frontend whole-document save; all mutations named backend commands. |
| REQ-104 | Verified | Command schema requires commandId/expectedRevision/type/typed payload; no ETag authority. |
| REQ-105 | Verified | Identical retry returns original result; differing reuse conflicts. |
| REQ-106 | Verified | Stale expected revision returns current revision/project with no apply. |
| REQ-107 | Verified | SQLite immediate transaction commits project/revision/issues/event-linked result or rolls back. |
| REQ-108 | Verified | Result includes authoritative project/issues/review/affected ranges/event cursor. |
| REQ-109 | Verified | Command union contains all required metadata/source/clip/sync/provenance/program/audio/anchor/suggestion/archive mutations. |
| REQ-110 | Implemented | Read resources expose capabilities/grants/libraries/scans/media/evidence/projects/program/suggestions/jobs/plans/reviews/artifacts. |
| REQ-111 | Verified | OpenAPI route-family contract test enumerates minimum surface. |
| REQ-112 | Verified | Stable safe error envelope with request ID/retryable/details. |
| REQ-113 | Verified | Required stable error-code enum in API schema and mapped HTTP statuses. |
| REQ-114 | Verified | Durable job states exactly implement required state machine. |
| REQ-115 | Verified | Sequence/timestamp/type/progress/message/details event rows on all transitions. |
| REQ-116 | Implemented | SSE primary connected UI, polling fallback without workflow loss. |
| REQ-117 | Verified | Separate normative `events.md` covers framing/replay/reconnect/token/terminal behavior. |
| REQ-118 | Verified | Restart interrupts/restarts idempotent work; renders never reattach; partial reconciliation. |
| REQ-119 | Verified | Non-blocking state-directory process lock; concurrent startup fails safely. |
| REQ-120 | Verified | Backend exclusively derives compilation/issues/preflight/manifest/FFmpeg plan. |
| REQ-121 | Verified | CLI rejects non-loopback host; no supported remote mode. |
| REQ-122 | Verified | Sensitive reads/mutations need server-expiring session; health has no metadata/path. |
| REQ-123 | Verified | One-time bootstrap redacted from logs and replaced by HttpOnly cookie. |
| REQ-124 | Verified | Host/Origin/fetch metadata/CSRF/restrictive CORS+CSP tests and headers. |
| REQ-125 | Verified | Session-bound short-lived SSE open token; unrelated/no-session test denied. |
| REQ-126 | Implemented | 2 MB HTTP, 500 page, 1 MB sidecar, bounded path/name/tool output/replay/concurrency. |
| REQ-127 | Verified | `shell=False` argument arrays; injection fixture/security inspection. |
| REQ-128 | Verified | Route-only logs; redacted bootstrap; generic media errors; no body/root/sidecar logging. |
| REQ-129 | Implemented | No package deps; runtime floors/observations/licenses/upgrade matrix documented. |
| REQ-130 | Verified | Collision-safe fingerprints; 10,000/2 GiB registered unpinned cache eviction only. |
| REQ-131 | Verified | 100,000-event finite retention; bounded tool diagnostics and route-only application logs. |
| REQ-132 | Verified | SQLite online backup/integrity/dry-run/locked restore/rollback tool and tests. |
| REQ-133 | Implemented | UI/error codes distinguish validation/grant/missing/stale/unsupported/failure/cancel/interrupted/recoverable. |
| REQ-134 | Implemented | Semantic buttons/selects, timeline focus, keyboard nudge/playhead/cut/review/job controls. |
| REQ-135 | Implemented | Textual labels/status/issue lanes/live regions/visible focus/non-color semantics; browser audit gate. |
| REQ-136 | Implemented | Numeric microsecond-derived offset/rate/audio/boundary controls and configurable key increments. |
| REQ-137 | Verified | Synthetic 26,520/1,000 benchmark meets read/command/replay/memory targets; corpus report covers first progress. |
| REQ-138 | Verified | Probe workers/queue plus one scan/library, two analyses, one full-hash plan, and one render; excess receives tested backpressure. |
| REQ-139 | Implemented | Shutdown cancels scans/renders, terminates process trees, persists transitions; startup reconciles. |
| REQ-140 | Implemented | Synthetic media matrix covers geometry/rotation/SAR/VFR/frame/color/HDR/audio/malformed/path/source-change. |
| REQ-141 | Verified | Legacy data migrates; original unknown document retained under recovery field. |
| REQ-142 | Verified | Float/millisecond time converts half-even to integer µs; originals/migration version retained. |
| REQ-143 | Verified | Legacy assets become separate initial sources/clips; same-camera candidate never silently merges. |
| REQ-144 | Verified | Positional audio becomes independent blocks; null becomes silence only with explicit legacy evidence. |
| REQ-145 | Verified | Migrated review is always null and requires new full-hash plan/attestation. |
| REQ-146 | Verified | Schema version/ledger, pre-migration online backup, transactional DDL, dry-run and restart-safe state. |
| REQ-147 | Verified | Verified backup precedes irreversible schema/restore; failure retains prior/rollback DB. |
| REQ-148 | Verified | Legacy whole-project HTTP writes removed; generated command client is sole frontend writer. |
| REQ-149 | Implemented | Legacy reader/importer remains only as migration/recovery seam. |
| REQ-150 | Verified | Baseline preserved in isolated worktree; branch pushed and pull request #2 opened under explicit authority; no merge/package/deploy. |
