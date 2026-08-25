# Design fidelity ledger

Compared accepted Open Design Cut surface at 1200 px with implementation Cut surface at 1200 px using Browser screenshots and direct image inspection.

| Point | Accepted evidence | Implementation evidence | Disposition |
|---|---|---|---|
| Workflow | Four compact Library/Align/Cut/Review steps | Same four steps, same order and active underline | Matched |
| Layout | Source rail, sole Program Monitor, inspector, Program lanes, source evidence | Same three-column hierarchy and vertical timeline stack | Matched; responsive rail widths tightened for 1200 px |
| Palette | Warm charcoal, bone text, restrained teal and amber | Same semantic palette and low-glare surfaces | Matched |
| Typography | Condensed headings, compact monospace timing/provenance | Condensed fallback headings and monospace chrome | Matched without external fonts |
| Program model | One camera on Program, separate Program Video/Audio | Same, driven by real indexed media and persisted decisions | Matched and functional |
| Anchoring/reconciliation | Wall-clock or source-clip mode; explicit gap/overlap repair | Same modes and non-silent repair controls | Matched |
| Provenance/fidelity | Inspectable source/segment data and honest re-encode disclosure | Real evidence records, manifest, preflight, and FFmpeg output | Matched and extended |

Intentional differences:

- Product name generalized from Home Security Video Alignment to Room Alignment.
- Vendor-specific sample counts and fixed camera names replaced with indexed local evidence.
- Implementation uses dependency-free system font fallbacks.
- Program Monitor is shorter in Cut view to keep real timeline controls visible on 1200 px desktop viewport.

No material visual mismatch remains for implemented desktop surface. Mobile is an adaptive fallback, not accepted primary target.
