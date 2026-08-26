# ADR 0005: Immutable event and session cluster generations

Status: Accepted

## Context

The browser grouped only the loaded media page into rolling date cards. Grouping
was incomplete, non-reproducible, unable to select multiple clusters, and hid
single-source groups.

## Decision

The backend is the sole clustering authority. A generation binds one catalog
revision, algorithm version, event gap, session gap, and configuration digest.
A streaming sweep uses the running maximum coverage end. More than 15 seconds
without coverage starts an event; more than 120 seconds starts a session. Defaults
are library settings and explicit changes create future generations.

Unknown-time assets remain unclustered. Low-confidence timing may suggest
membership but cannot establish accepted alignment. A project snapshot stores
selected cluster IDs, exact assets, manual adjustments, and a digest.

## Consequences

- Sessions and nested events are stable paginated resources.
- Single-source events remain selectable and labelled.
- Multi-event, multi-session, multi-date, and multi-root projects are supported.
- Work is an indexed sort plus one streaming sweep with bounded active state.
