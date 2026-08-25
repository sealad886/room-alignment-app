# Risk register

| ID | Risk | Probability / impact | Response | State |
|---|---|---|---|---|
| RISK-001 | Timestamp inference is wrong or absent | Medium / High | Preserve evidence/confidence; require reversible user alignment | Mitigated in model; richer correction UI planned |
| RISK-002 | Vendor-specific filename logic becomes hidden contract | Medium / High | Generic parsers; unknown valid; custom fields retained; corpus tests | Mitigated |
| RISK-003 | FFmpeg parser vulnerability | Low / High | Current FFmpeg, local trusted corpus expectation, subprocess isolation boundary documented | Residual |
| RISK-004 | Render overwrites existing output | Medium / Medium | Existing outputs rejected; temporary render and atomic promotion | Mitigated; desktop shell may add explicit recoverable overwrite flow |
| RISK-005 | Very large scan is slow | High / Medium | Bounded initial scan, background progress, persistent SQLite | Partial: incremental fingerprint skip is next optimization |
| RISK-006 | Differing video geometry breaks concat | Medium / High | Preflight metadata available | Open: renderer normalization policy needs broader corpus matrix |
| RISK-007 | Local hostile page calls loopback API | Low / High | Loopback, local Host validation, same-origin mutation check | Residual: add per-launch origin token before packaged release |
