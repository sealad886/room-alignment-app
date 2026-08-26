# Local operation and recovery runbook

## Start and stop

```bash
room-alignment serve --no-open --data-dir /path/to/state
room-alignment stop --data-dir /path/to/state
```

Health is intentionally non-sensitive at `/api/health`. A second process using the same state directory fails before scheduling work. Graceful shutdown rejects new work, requests scan cancellation, terminates owned render process trees, persists job transitions, and releases ownership. On restart, in-flight analyses/scans are `INTERRUPTED`; renders are never blindly reattached and become `FAILED_RECOVERABLE` where appropriate.

`room-alignment stop` reads process identity only from the contended state lock; it does not search or signal processes by name. Graceful shutdown clears that identity while still holding the lock, then unlocks and closes the file. It is idempotent when no process owns that directory. Use `--timeout SECONDS --force` only when graceful shutdown cannot complete; force mode sends `SIGKILL` to the still-validated lock owner after the timeout.

Run `room-alignment doctor` before first use or after changing Python/FFmpeg. It checks installed frontend/schema resources and FFmpeg/FFprobe availability without exposing absolute paths.

## Application settings

Settings are available from every workflow phase and persist in `room-alignment.sqlite3`. Overlap search extends timestamp-derived clip ranges before and after each clip and applies the same bound to audio-correlation lag search. Default is 30 seconds; allowed range is 0–300 seconds. Increasing it may prepare more audio signatures and comparisons, but analysis remains limited to eight candidates per clip and 2,000 pairs per job. A changed overlap setting marks pending alignment proposal sets stale; run **Analyze overlaps** again. Text size and color scheme changes are presentation-only.

Build/install validation uses a temporary virtual environment and state directory:

```bash
uv build
python3 scripts/verify_package.py dist/room_alignment-0.3.0-py3-none-any.whl
```

The verifier installs without network access, starts on an ephemeral loopback port, validates authenticated resources, sends SIGTERM, and reuses the same state directory. It never scans a media library.

## Scan diagnosis

Individual unreadable/malformed assets remain warning-bearing records. Full scan alone may mark unobserved prior assets missing; bounded/incremental scans never do. Cancel is idempotent and retains visited records. Grant revocation interrupts dependent work without deleting project decisions. Re-grant/rescan is a new explicit action.

## Render diagnosis

Resolve canonical blocker codes in Review. Existing output/manifest, insufficient initial or continuing free space, source hash change, stale review/plan, missing media/audio, coverage/ambiguity/sync issues, unsupported required transforms, and revoked grants prevent completion. Cancel terminates the process group and removes only the exact owned partials. Startup moves owned partials to `state/recovery` and records their names; one-file final pairs are never called complete.

## Backup and restore

Application backup is canonical SQLite state only; source libraries and rendered outputs remain separate.

```bash
room-alignment admin backup STATE.sqlite3 BACKUP.sqlite3
room-alignment admin verify BACKUP.sqlite3
room-alignment admin dry-run-migrate BACKUP.sqlite3
```

Stop the application before restore. Restore verifies the input, refuses without `--replace`, takes the state lock, creates a verified `*.pre-restore-*` rollback copy, stages the replacement, and atomically replaces the DB.

```bash
room-alignment admin restore STATE.sqlite3 BACKUP.sqlite3 --replace
```

After restore, start normally, inspect projects/jobs/grants, and regrant unavailable source/output directories as needed. Restore never assumes media was included.

## Retention

Canonical projects, evidence, corrections, plans/reviews, jobs, and artifacts are not cache entries. Recent job events are compacted to 100,000. Registered derived cache entries are limited to 10,000/2 GiB; only unpinned exact files under `state/cache` are evicted. Quarantined recovery files require user inspection before deletion.
