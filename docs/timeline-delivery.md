# Timeline, clustering, and multi-folder delivery contract

The accepted delivery keeps `Library → Align → Cut → Review` and makes project
preparation evidence-first.

## Locked invariants

- A named library has zero to sixteen independently granted read-only roots.
- Asset identity includes its root. Equal relative paths across roots are distinct;
  duplicate-content evidence never merges identities silently.
- Sessions contain events. Immutable generations bind a catalog revision plus
  15-second event and 120-second session gap defaults.
- A selection snapshot stores exact assets and a digest. Later scans or generations
  never change membership.
- New projects have no program blocks. Timestamp placement is provisional until
  accepted manually or through a server-owned proposal set.
- Source time maps to aligned evidence time, then explicit keep/exclude/slate
  sections map to program time. A slate generates video and deliberate silence.
- First-cut generation binds selection, alignment, and gap decisions and optimizes
  coverage before confidence, fidelity, switches, transforms, and stable identity.
- Timeline reads return at most 2,000 exact items or aggregate buckets.
- Legacy truncated programs are repaired only after preview plus confirmed
  `GenerateProgramDraft(replaceExisting=true)`.

## Normative contract locations

- `contracts/timeline.schema.json`: roots, catalogs, clusters, selections,
  alignment, sections, windows, draft plans, and slates.
- `contracts/domain.schema.json`: project and clip composition.
- `contracts/commands.schema.json`: named alignment, section, and draft commands;
  `InitializeProgram` is deprecated.
- `contracts/api.schema.json` and `contracts/openapi.json`: HTTP resources and
  generated-client operations.

## Finding policy

P0/P1 correctness, security, data-integrity, migration, and regression findings
block advancement. P2/P3 findings are logged in `docs/risk-register.md` with a
follow-up disposition rather than expanding the active milestone automatically.
