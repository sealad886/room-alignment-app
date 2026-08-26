# Local operation and recovery runbook

## Start and stop

```bash
python3 -m room_alignment.server --no-open --data-dir /path/to/state
```

Health is intentionally non-sensitive at `/api/health`. A second process using the same state directory fails before scheduling work. Graceful shutdown rejects new work, requests scan cancellation, terminates owned render process trees, persists job transitions, and releases ownership. On restart, in-flight analyses/scans are `INTERRUPTED`; renders are never blindly reattached and become `FAILED_RECOVERABLE` where appropriate.

## Scan diagnosis

Individual unreadable/malformed assets remain warning-bearing records. Full scan alone may mark unobserved prior assets missing; bounded/incremental scans never do. Cancel is idempotent and retains visited records. Grant revocation interrupts dependent work without deleting project decisions. Re-grant/rescan is a new explicit action.

## Render diagnosis

Resolve canonical blocker codes in Review. Existing output/manifest, insufficient initial or continuing free space, source hash change, stale review/plan, missing media/audio, coverage/ambiguity/sync issues, unsupported required transforms, and revoked grants prevent completion. Cancel terminates the process group and removes only the exact owned partials. Startup moves owned partials to `state/recovery` and records their names; one-file final pairs are never called complete.

## Backup and restore

Application backup is canonical SQLite state only; source libraries and rendered outputs remain separate.

```bash
python3 scripts/state_admin.py backup STATE.sqlite3 BACKUP.sqlite3
python3 scripts/state_admin.py verify BACKUP.sqlite3
python3 scripts/state_admin.py dry-run-migrate BACKUP.sqlite3
```

Stop the application before restore. Restore verifies the input, refuses without `--replace`, takes the state lock, creates a verified `*.pre-restore-*` rollback copy, stages the replacement, and atomically replaces the DB.

```bash
python3 scripts/state_admin.py restore STATE.sqlite3 BACKUP.sqlite3 --replace
```

After restore, start normally, inspect projects/jobs/grants, and regrant unavailable source/output directories as needed. Restore never assumes media was included.

## Retention

Canonical projects, evidence, corrections, plans/reviews, jobs, and artifacts are not cache entries. Recent job events are compacted to 100,000. Registered derived cache entries are limited to 10,000/2 GiB; only unpinned exact files under `state/cache` are evicted. Quarantined recovery files require user inspection before deletion.
