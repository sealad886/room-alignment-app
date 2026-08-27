# ADR 0007: Separate alignment evidence, program eligibility, and coverage readiness

Status: Accepted

## Context

Large projects can contain hundreds of timestamp-only clips and disconnected audio-overlap groups. Requiring every selected clip to be accepted before program generation makes useful coverage unreachable and conflates timing confidence with editorial eligibility.

## Decision

Persist program eligibility independently from alignment state. Compute preparation readiness by sweeping explicit Keep, Exclude, and Slate sections: only sole-coverage uncertainty blocks. Timestamp-prior proposals may be accepted in a scoped, preview-bound atomic command; conflicts never participate. Audio evidence is solved per connected component, with relative and absolute confidence reported separately.

## Consequences

Existing accepted clips migrate to `ELIGIBLE`; all other clips migrate to `HELD_FOR_REVIEW`. Pending proposal sets become stale when algorithm version changes. Manual transforms and existing programs remain unchanged. Compiler and render preflight continue validating exact selected slices.

## Alternatives rejected

- Requiring acceptance of every clip keeps large projects operationally blocked.
- Treating rejected proposals as excluded erases the distinction between evidence and editorial intent.
- One project-wide audio anchor incorrectly penalizes disconnected events.
- Client-side bulk transform submission creates a second rules engine and weakens auditability.
