# Requirements and acceptance

| ID | Requirement | Acceptance evidence |
|---|---|---|
| REQ-001 | Index common local video containers without vendor dependency | AC-001 unknown filename accepted; AC-002 folder/filename/container/sidecar evidence merged; unit + real scan |
| REQ-002 | Never mutate source media | AC-003 source operations are read/probe only; code/security audit and real corpus run |
| REQ-003 | Persist libraries and projects locally | AC-004 rescan upserts; AC-005 recent project reopens after page reload; browser/API test |
| REQ-004 | Align sources independently of editorial cuts | AC-006 reference and offsets persist; AC-007 wall-clock mode keeps cut intervals fixed |
| REQ-005 | Choose exactly one Program Video source per valid interval | AC-008 contiguous segments and monitor selection; browser test/preflight |
| REQ-006 | Select Program Audio independently | AC-009 link/unlink, alternate source, offset, and silence supported; integration render |
| REQ-007 | Expose clip-attached cut consequences | AC-010 offset change produces visible gap/overlap; AC-011 repair is explicit |
| REQ-008 | Preserve provenance and transformation truth | AC-012 manifest contains source-relative identity, evidence, ranges, offsets, transforms, fidelity plan |
| REQ-009 | Render safely and recoverably | AC-013 missing media/coverage/provenance blocks; AC-014 partial file promoted atomically; AC-015 cancellation removes partial; AC-016 existing output is not overwritten |
| REQ-010 | Preserve accepted polished desktop model | AC-017 Browser screenshot matches accepted hierarchy/palette/chrome; fidelity ledger |
| REQ-011 | Protect private local service | AC-018 loopback default; local Host and same-origin mutation checks; no remote dependencies |

Edge cases covered: unknown timestamps, opaque names, JSON sidecar custom fields, malformed media warning, source overrun, leading/internal gap, overlap, no audio/silence, differing video geometry, reload/reopen, and existing destination.

