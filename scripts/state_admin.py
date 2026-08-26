from __future__ import annotations

import argparse
import fcntl
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from room_alignment.store import Store


def _validate_database(path: Path) -> dict[str, int | str]:
    if not path.is_file():
        raise ValueError("Database file does not exist")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if result != "ok":
            raise ValueError(f"SQLite integrity check failed: {result}")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = int(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0])
        return {"integrity": result, "schemaVersion": version, "tables": tables}
    finally:
        connection.close()


def _online_backup(source: Path, destination: Path) -> dict[str, int | str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Backup destination already exists")
    source_db = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    destination_db = sqlite3.connect(destination)
    try:
        source_db.backup(destination_db)
    finally:
        destination_db.close()
        source_db.close()
    return _validate_database(destination)


def _exclusive_state_lock(state_database: Path):
    lock_path = state_database.parent / "application.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise ValueError("Stop the application before restoring canonical state") from error
    return lock


def backup(source: Path, destination: Path) -> dict[str, int | str]:
    _validate_database(source)
    return _online_backup(source, destination)


def dry_run_migration(source: Path) -> dict[str, int | str]:
    _validate_database(source)
    with tempfile.TemporaryDirectory(prefix="room-alignment-migration-") as temporary:
        copy = Path(temporary) / "state.sqlite3"
        _online_backup(source, copy)
        Store(copy)
        return _validate_database(copy)


def restore(state_database: Path, backup_database: Path, replace: bool) -> dict[str, int | str]:
    if not replace:
        raise ValueError("Restore requires --replace because it changes canonical state")
    _validate_database(backup_database)
    lock = _exclusive_state_lock(state_database)
    try:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rollback = state_database.with_name(f"{state_database.name}.pre-restore-{timestamp}")
        if state_database.exists():
            _online_backup(state_database, rollback)
        staging = state_database.with_name(f".{state_database.name}.restore-{os.getpid()}")
        try:
            _online_backup(backup_database, staging)
            os.replace(staging, state_database)
            for suffix in ("-wal", "-shm"):
                Path(f"{state_database}{suffix}").unlink(missing_ok=True)
                Path(f"{staging}{suffix}").unlink(missing_ok=True)
        finally:
            staging.unlink(missing_ok=True)
        details = _validate_database(state_database)
        details["rollbackCreated"] = int(rollback.exists())
        return details
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up, verify, migration-check, or restore local canonical state")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("database", type=Path)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("database", type=Path)
    backup_parser.add_argument("destination", type=Path)
    migration_parser = subparsers.add_parser("dry-run-migrate")
    migration_parser.add_argument("database", type=Path)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("database", type=Path)
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.command == "verify":
        result = _validate_database(args.database)
    elif args.command == "backup":
        result = backup(args.database, args.destination)
    elif args.command == "dry-run-migrate":
        result = dry_run_migration(args.database)
    else:
        result = restore(args.database, args.backup, args.replace)
    print(" ".join(f"{key}={value}" for key, value in sorted(result.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
