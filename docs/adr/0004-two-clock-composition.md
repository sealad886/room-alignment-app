# ADR 0004: Separate aligned evidence time from program time

Status: Accepted

## Context

The original model used `SyncTransform.anchorOutputUs` for both clip placement
and edited output. Project creation also generated program blocks immediately.
A short early clip could therefore define program duration before later evidence
was aligned, hiding most selected media.

## Decision

Projects distinguish source-relative time, `alignedTimeUs` on the complete
selected-evidence timeline, and `programTimeUs` after gap decisions.
`ClipAlignmentTransform` maps source time to aligned time. `TimelineSection`
maps kept or synthetic aligned ranges to program time; excluded ranges have no
program mapping.

New projects contain no Program Video or Program Audio blocks. Program creation
requires a bound selection digest, alignment digest, explicit gap decisions, and
a `GenerateProgramDraft` preview/commit. Legacy `anchorOutputUs` remains readable
as migration input and is deprecated in the public contract.

## Consequences

- The evidence timeline can display every selected clip before a program exists.
- Excluding downtime cannot change source alignment evidence.
- Review and render can bind both clocks and their mapping.
- Existing projects retain exact legacy values and require explicit repair when
  their program does not cover aligned media.
