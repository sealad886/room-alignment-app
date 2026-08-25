# Delivery plan and disposition

## Completed milestones

- MS-1 — Versioned domain, command, error, event, render-plan, and manifest contracts; generated browser client; architecture decisions.
- MS-2 — Secure loopback session, opaque grants, durable SQLite state/jobs/events, restart recovery, bounded caches, and verified migration/backup seam.
- MS-3 — Vendor-neutral scan generations, permissive evidence, timestamp policies, logical sources, source candidates, and read-only representative-corpus validation.
- MS-4 — Canonical project commands, affine synchronization, independent video/audio blocks, backend compilation/issues, point query, and connected Align/Cut flows.
- MS-5 — Optional asynchronous timestamp/cluster suggestions with evidence, limitations, lifecycle, and invalidation; manual alignment remains complete without them.
- MS-6 — Immutable full-hash plans, review attestations, compatible/lossless render profiles, source revalidation, crash-safe artifact pairs, and provenance manifests.
- MS-7 — Legacy cutover, 150-requirement disposition ledger, full tests, browser/accessibility/visual checks, performance benchmark, real-corpus integrity validation, and adversarial remediation.

Critical path completed: source safety → canonical index/provenance → revisioned alignment/editing → immutable review → render/recovery → representative evidence.

## Local release-candidate gate

The local source release candidate is accepted only when all of the following remain true:

- `docs/requirements-ledger.md` maps every `REQ-001` through `REQ-150` without silent waiver.
- `docs/verification-report.md` records a current full-suite, contract/static, browser, performance, render, and corpus result.
- `docs/adversarial-audit.md` has no open critical/high correctness, security, data-integrity, contract, accessibility, migration, or recovery finding.
- Source media integrity is unchanged and no private path/name/media is committed.
- Frontend uses named command APIs and the generated client; backend remains the only compiler/preflight/render authority.

## Delivery state

Implementation is complete and locally verified in the isolated `codex/technology-agnostic-backend` worktree. Milestone commits are local only. No push, pull request, package, installation, signing, notarization, publication, deployment, distribution, or production observation was performed or implied.
