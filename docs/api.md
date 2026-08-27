# Local API v1

`contracts/openapi.json` is normative for HTTP operations. `contracts/api.schema.json`, `domain.schema.json`, `commands.schema.json`, `timeline.schema.json`, and `manifest.schema.json` define resource, domain, command, timeline, and completed-manifest structures. The service exposes those exact files at `/api/v1/openapi.json` and `/api/v1/contracts/{name}`. `web/api-client.js` is generated from operation IDs and fails verification when stale.

All sensitive reads and every mutation require the bootstrapped local session. Mutations additionally require the session CSRF token. Non-success responses use the stable `ErrorEnvelope` schema. `expectedRevision` is the only mutation precondition; ETag is not used.

## Route families

- `/system`, `/session`, `/openapi.json`, `/contracts/{contractName}`
- `/grants`, `/grants/{grantId}/revoke`
- `/libraries`, `/libraries/{libraryId}/scans`, `/libraries/{libraryId}/time-policy`, `/libraries/{libraryId}/cluster-jobs`, `/libraries/{libraryId}/cluster-suggestions`
- `/libraries/{libraryId}/roots`, root revocation, immutable cluster generations,
  and paginated session/event resources
- `/scans/{scanId}`, `/scans/{scanId}/cancel`
- `/libraries/{libraryId}/media`, `/media/{mediaId}`, `/media/{mediaId}/preview`, `/media/{mediaId}/provenance/resolutions`
- `/projects`, `/projects/{projectId}`, `/projects/{projectId}/commands`, `/projects/{projectId}/program`, `/projects/{projectId}/program-at`, `/projects/{projectId}/suggestions`, `/projects/{projectId}/alignment-jobs`, `/projects/{projectId}/render-plans`
- project alignment summary, windowed evidence timeline, alignment proposal sets,
  preview-bound alignment acceptance, and delta command results
- `/render-plans/{planId}`, `/render-plans/{planId}/review`, `/render-plans/{planId}/render`
- `/jobs/{jobId}`, `/jobs/{jobId}/cancel`, `/jobs/event-token`, `/events`
- `/artifacts/{artifactId}`, `/artifacts/{artifactId}/video`, `/artifacts/{artifactId}/manifest`

## Commands and concurrency

Every project mutation has `{commandId, expectedRevision, commandType, payload}`. A committed result includes previous/applied revision, authoritative project, canonical issues, review state, event cursor, and compiled `affectedIntervals`. An identical replay returns the original result; reusing the ID with different content returns `IDEMPOTENCY_CONFLICT`; stale revision returns `REVISION_CONFLICT` with the current project and revision; no failure partially applies.

Project creation may include `sourceGroups`, an exact partition of the selected asset IDs. This is accepted only after the UI has asked the user to confirm the proposed source identities. `AcceptAlignmentSuggestions` applies an explicit list of evidence-bearing timestamp transforms in one project revision so the first accepted transform cannot stale the remainder of the same reviewed batch.

New project creation is evidence-only. `InitializeProgram` is deprecated and kept
only for migration compatibility; `GenerateProgramDraft` is the authoritative
first-cut command and binds selection plus alignment digests.

`POST /projects/{projectId}/alignment-proposal-acceptance-previews` creates a short-lived, revision-bound preview for scoped high-confidence or timestamp-prior acceptance. Timestamp-prior `AcceptAlignmentProposalSet` commands bind its ID and digest and explicitly confirm timestamp uncertainty. Any project, proposal, scope, or expiry mismatch rejects the whole command.

`SetClipProgramEligibility` and `SetRangeProgramEligibility` change optimizer eligibility without changing timing evidence. A clip must have accepted timing before it can become eligible.

## Events

`contracts/events.md` is normative for SSE framing and replay. The UI requests a session-bound token, reconnects from its last sequence, and falls back to polling when SSE is unavailable. Connections deliberately recycle; a new short-lived token is acquired for each reconnect. Canonical terminal state remains queryable from `/jobs/{jobId}` even after finite event retention compacts older progress events.

## Filesystem authority

The grant-creation operation is the only ordinary API that accepts a directory path, and it is invoked only by an explicit user action. All subsequent requests use opaque grant IDs. Source preview resolves the current media record beneath its current read-only grant on every request, requires the local session, supports one HTTP byte range for browser seeking, and never exposes the stored root. Artifact video/manifest retrieval resolves recorded filenames beneath the current output grant and streams content without exposing the stored root.
