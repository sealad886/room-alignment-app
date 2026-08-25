# Security and privacy

## Assets

- Private video/audio content
- Location/camera labels and timestamps
- Source paths and provenance
- Editorial decisions and rendered outputs

## Trust boundaries

- Browser ↔ loopback HTTP service
- Service ↔ selected source directories
- Service ↔ FFprobe/FFmpeg subprocesses
- Service ↔ output directory

## Threats and controls

| Threat | Control |
|---|---|
| Path traversal | Resolve path; require source remains beneath canonical library root |
| Source mutation | Scanner/prober only read; rendering reads inputs and writes separate destination |
| Symlink escape | Directory walk does not follow directory symlinks; render revalidates canonical containment |
| Malformed media | FFprobe timeout, bounded error text, no shell invocation |
| Command injection | FFmpeg launched as argument array, never shell string |
| Partial/corrupt final output | Render to sibling `.partial` path, atomically replace only on success |
| Privacy leakage | Loopback default; local Host and same-origin mutation checks; no external requests; manifests use relative source paths |
| Oversized request | JSON request capped at 10 MB |
| Unsafe render | Preflight blocks gaps, overlaps, missing files, provenance omissions, and source overruns |

Residual risks: local processes running as same user can access service and SQLite; FFmpeg remains complex native parser. Existing output files are rejected. Desktop packaging should add per-launch origin token/capability binding and a recoverable overwrite-confirmation flow.
