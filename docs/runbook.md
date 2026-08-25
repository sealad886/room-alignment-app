# Runbook

## Start

```bash
python3 -m room_alignment.server --no-open
```

Health: `GET http://127.0.0.1:8765/api/health`.

## State

Default database: `~/.room-alignment/room-alignment.sqlite3`. Back up by stopping service and copying SQLite file. Source media is not part of application backup.

## Scan failure

Check folder exists and is readable. Individual malformed videos become record warnings. Whole-scan failure appears in scan status. Retry is safe and upserts stable media IDs.

## Render failure

Review UI/status message. Source availability and segment coverage are revalidated immediately before FFmpeg launch. Failed/canceled jobs remove partial output. Existing successful final output is not removed.

## Recovery

- Reopen same library: stable ID derives from canonical root.
- Re-scan: records upsert by stable library-relative ID.
- Restart after interrupted render: partial file may remain only after abrupt process kill; delete only exact `*.partial.<ext>` after verifying no render process owns it.
- Database corruption: restore stopped-service backup or move exact DB aside and rescan. Never delete source library.

## Verification

Run unit/static checks from README. For release candidate, index multiple vendor/layout corpora read-only, exercise both anchoring modes, render H.264/AAC and FFV1/PCM, inspect outputs with FFprobe, and compare manifest intervals to decoded frames/audio.

