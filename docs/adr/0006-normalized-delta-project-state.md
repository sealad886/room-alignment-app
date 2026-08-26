# ADR 0006: Normalize scalable project state and return command deltas

Status: Accepted

## Context

Canonical projects are stored as one JSON document and every command copies that
document into history and the response. This migration seam does not scale to
10,000 clips or frequent timeline edits.

## Decision

Roots, catalogs, clusters, memberships, clips, alignment transforms, timeline
sections, and program blocks use normalized SQLite tables. Commands remain the
sole mutation authority and store atomic deltas plus periodic snapshots. Immutable
full snapshots remain at review and render boundaries. A delta endpoint returns
changed entities, project summary, issue delta, revision, and event cursor.

Legacy documents remain readable during bounded cutover. Writes are disabled only
after generated-client and connected-UI parity. Migration uses the verified
backup, staged database, integrity check, and atomic replacement workflow.

## Consequences

- Ordinary edits avoid serializing an entire large project.
- Review/render inputs remain reproducible.
- Migration stays reversible and never rebuilds projects silently.
- Normalized rows and compatibility snapshots must be transactionally consistent.
