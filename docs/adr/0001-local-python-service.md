# ADR 0001: Dependency-light local service

Status: Accepted.

Use Python standard-library loopback service with static web UI. This supports real filesystem access, avoids third-party Python/browser runtime dependencies, and packages as one platform-independent wheel. Hatchling is pinned and build-only; installed backend/frontend/contracts remain one distribution. Rejected browser-only File System Access because support and persistent directory authority vary.
