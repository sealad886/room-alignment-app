# Project brief

## Problem and outcome

Multiple cameras record disjoint, partially overlapping clips. User needs one synchronized, conventional single-view output, with independently selectable audio and evidence linking every output interval to original media.

Desired outcome: runnable local application that indexes arbitrary video libraries, supports reversible alignment and editorial decisions, validates coverage/provenance/fidelity, and produces video plus manifest without modifying sources.

## Scope

- Local library indexing and persistent projects
- Vendor-agnostic provenance evidence
- Manual source alignment
- Single-source Program Video and independent Program Audio
- Cut anchoring and explicit boundary reconciliation
- Preflight, manifest, MP4/lossless render, cancellation/recovery
- Accepted desktop UI and local documentation/tests

Non-scope: cloud sync, user accounts, automatic visual/audio alignment, identity recognition, mobile-native UI, signing/notarization, distribution, and deployment.

## Constraints and decisions

- User requirement: do not make product Blink-specific; provenance must be permissive.
- User requirement: complete safe implementation without further input where possible.
- Accepted Open Design artifact is visual/product authority.
- Source video is private and immutable.
- Dependency-light Python/HTML implementation is agent decision for runnable local delivery.

## Success

- Unknown layouts index without rejection.
- Representative real corpus groups by inferred date/source while remaining read-only.
- Library → Align → Cut → Review works in browser.
- Gaps/overlaps or missing coverage block render.
- Real single-view output and provenance manifest complete.
- Unit, integration, static, browser, and visual checks pass.

Definition of Ready: accepted design, source corpus, safety boundary, and implementation authority are present. Definition of Done: implemented scope passes listed checks, documentation matches behavior, residual risks are explicit, and no publication/deployment is claimed.

