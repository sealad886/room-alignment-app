# DPIA-lite

## Processing

Room Alignment processes locally stored security or general multi-camera video metadata and, when rendering, media content. Purpose is synchronization, editorial source selection, provenance, and local output creation.

## Necessity and proportionality

Only filename/path context, filesystem facts, selected container metadata, sidecar fields, and user decisions are persisted. Full media is not copied into application database. User chooses source and output locations.

## Data subjects and risks

Footage may contain household members, visitors, employees, or public passers-by. Timestamps, locations, audio, and camera labels can be sensitive. Main risks are unintended disclosure, excessive retention, misleading provenance, and destructive editing.

## Safeguards

- Local-only processing; no cloud transfer
- Read-only sources
- Relative paths in manifests
- Evidence confidence and origin retained
- Explicit review before rendering
- Separate project-state deletion possible without deleting source media
- No identity recognition, tracking, or biometric processing

User remains responsible for lawful collection, retention, sharing, and export of footage in their jurisdiction.

