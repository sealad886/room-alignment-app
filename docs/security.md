# Security and privacy

## Boundary and assets

The supported deployment is a single local user and a loopback-only HTTP service. Sensitive assets are footage/audio, timestamps, source identities, relative/absolute paths, provenance, decisions, output, manifests, and local session capability. Core workflow makes no external network request and emits no telemetry.

## Controls

- A one-time bootstrap URL is redacted from request logs, becomes unusable immediately, and creates an HttpOnly/SameSite=Strict session with both browser and server 12-hour expiry.
- Sensitive API reads and mutations validate loopback Host, same-origin Origin when present, and fetch metadata. Mutations require CSRF. CORS is not opened. CSP disallows remote/script-inline/object/frame/form destinations; inline style is permitted only because data-driven timeline geometry uses style properties.
- SSE tokens are random, session-bound, expire within 60 seconds for opening a connection, and are never logged. Reconnection obtains another token.
- Source/output roots require explicit role grants, may not overlap, and are absent from public grant resources. Every access resolves beneath the current root; traversal, symlink, alias, and mount/path changes fail closed.
- Discovery does not follow directory symlinks. Unknown extensions require a recognized container signature before probe. Sidecars are capped at 1 MB; HTTP bodies at 2 MB; media-tool stdout/stderr, pagination, replay, names, and concurrency are bounded.
- FFprobe/FFmpeg use argument arrays, `shell=False`, no stdin, process groups, timeouts/cancellation, and bounded persisted diagnostics. Media errors do not persist source paths or arbitrary tool output.
- Full SHA-256 source identity is computed for render authorization and revalidated before and after processing. Existing output is never overwritten. Continuing disk-space floor, owned partial names, atomic promotion, pair reconciliation, and output/manifest digests protect artifact integrity.
- Logs contain route templates/status only, not bodies, credentials, source roots, media contents, or sidecar payloads. Default manifests contain stable IDs, library-relative paths, selected evidence resolutions, and required provenance only.

## Residual risk

Same-OS-user malware may already read the same files or process memory. FFmpeg/FFprobe remain a complex native parser surface. Loopback defenses protect browser-origin/application authority but do not create an OS sandbox. Non-loopback binding, remote collaboration, accounts, and cloud sync are unsupported without a new authenticated architecture and threat review.
