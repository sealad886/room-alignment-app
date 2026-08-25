# Compliance-oriented control mapping

This is engineering guidance and evidence mapping, not certification or legal advice.

| Theme | Implemented control and evidence |
|---|---|
| Data minimization | Local metadata/decision index; no footage copy in canonical DB; manifest exports selected relative identity and resolutions, not roots or wholesale sidecars. |
| Integrity and confidentiality | Loopback-only authenticated service; Host/Origin/fetch-metadata/CSRF/CSP controls; opaque non-overlapping read/write grants; source-containment checks. |
| Transparency and accuracy | Conflicting evidence retained; explicit resolution ledger; confidence/uncertainty/algorithm limitations; complete transformation/fidelity manifest. |
| Input and process safety | 2 MB HTTP, 1 MB sidecar, bounded page/replay/tool-output/concurrency; structured subprocess argument arrays; no shell evaluation. |
| Source/file handling | No source writes; symlink escape fails closed; full hashes before review and before/after render; grant revocation stops dependent work. |
| Artifact integrity | Existing destinations rejected; continuing disk floor; exact owned partials; two-file completion rule; digests and startup reconciliation. |
| Diagnostics/privacy | Route/status-only logs; credentials/query values/roots/media/sidecar payloads excluded; client disconnects do not print tracebacks. |
| Availability/recovery | Durable jobs/events, finite retention, process ownership, graceful bounded shutdown, restart reconciliation, verified backup/dry-run/restore. |
| Change integrity | Normative schemas, generated-client drift check, 150-requirement ledger, automated tests, browser audit, performance/corpus evidence, adversarial closure ledger. |
| Accessibility | Keyboard/numeric timeline operations, semantic controls and live regions, non-color status, visible focus, WCAG AA contrast audit, responsive 800px check. |
