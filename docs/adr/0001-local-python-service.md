# ADR 0001: Dependency-light local service

Status: Accepted.

Use Python standard-library loopback service with static web UI. This supports real filesystem access immediately, avoids package supply-chain/runtime setup, and preserves desktop packaging seam. Rejected browser-only File System Access because support and persistent directory authority vary.

