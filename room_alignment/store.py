from __future__ import annotations

import base64
import copy
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .domain import (
    DomainError,
    apply_command,
    compile_program,
    digest_json,
    new_project,
    now_iso,
    opaque_id,
    seconds_to_us,
)
from .models import MediaRecord, ScanSummary
from .models import ProvenanceEvidence
from .provenance import normalize_timestamp
from .scanner import media_record_from_dict, quick_fingerprint


SCHEMA_VERSION = 3
MAX_JOB_EVENTS = 100_000
MAX_CACHE_ENTRIES = 10_000
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
TERMINAL_JOB_STATES = {"CANCELED", "SUCCEEDED", "FAILED", "INTERRUPTED", "FAILED_RECOVERABLE"}
JOB_STATES = {
    "QUEUED",
    "RUNNING",
    "CANCEL_REQUESTED",
    "CANCELED",
    "SUCCEEDED",
    "FAILED",
    "INTERRUPTED",
    "FAILED_RECOVERABLE",
}
PROGRAM_AFFECTING_COMMANDS = {
    "MergeLogicalSources", "SplitLogicalSource", "ArchiveLogicalSource", "AssignClip", "SetReferenceSource",
    "SetSyncTransform", "InitializeProgram", "AddVideoBlock", "SplitVideoBlock", "MoveVideoBoundary",
    "DeleteVideoBlock", "AssignVideoSource", "PinVideoClip", "CutToSource", "AddAudioBlock",
    "SplitAudioBlock", "MoveAudioBoundary", "DeleteAudioBlock", "SetAudioMode", "SetAnchoringMode",
    "ReconcileBoundary", "AcceptAlignmentSuggestion",
}


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS directory_grants (
  id TEXT PRIMARY KEY,
  role TEXT NOT NULL CHECK(role IN ('READ_ONLY_SOURCE','WRITE_OUTPUT')),
  root TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS grants_active_root_role
  ON directory_grants(root, role) WHERE revoked=0;
CREATE TABLE IF NOT EXISTS libraries (
  id TEXT PRIMARY KEY,
  grant_id TEXT REFERENCES directory_grants(id),
  root TEXT NOT NULL UNIQUE,
  time_zone TEXT NOT NULL DEFAULT 'UTC',
  dst_fold INTEGER NOT NULL DEFAULT 0,
  nonexistent_policy TEXT NOT NULL DEFAULT 'REJECT',
  current_generation INTEGER NOT NULL DEFAULT 0,
  last_scan TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS scan_generations (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  generation INTEGER NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('FULL','INCREMENTAL','BOUNDED')),
  status TEXT NOT NULL,
  limit_count INTEGER,
  scanned INTEGER NOT NULL DEFAULT 0,
  videos INTEGER NOT NULL DEFAULT 0,
  warnings INTEGER NOT NULL DEFAULT 0,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  message TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(library_id, generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_scan_per_library
  ON scan_generations(library_id) WHERE status IN ('QUEUED','RUNNING','CANCEL_REQUESTED');
CREATE TABLE IF NOT EXISTS media (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  relative_path TEXT NOT NULL,
  captured_at TEXT,
  camera TEXT,
  duration REAL,
  first_generation INTEGER NOT NULL DEFAULT 0,
  last_generation INTEGER NOT NULL DEFAULT 0,
  missing INTEGER NOT NULL DEFAULT 0,
  fingerprint_json TEXT NOT NULL DEFAULT '{}',
  record_json TEXT NOT NULL,
  UNIQUE(library_id, relative_path)
);
CREATE INDEX IF NOT EXISTS media_library_path ON media(library_id, relative_path, id);
CREATE INDEX IF NOT EXISTS media_library_time ON media(library_id, captured_at);
CREATE INDEX IF NOT EXISTS media_library_camera ON media(library_id, camera);
CREATE TABLE IF NOT EXISTS media_identity_keys (
  asset_id TEXT PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
  library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  identity_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_identity_lookup ON media_identity_keys(library_id,identity_key);
CREATE TABLE IF NOT EXISTS provenance_resolutions (
  id TEXT PRIMARY KEY,
  media_id TEXT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  revision INTEGER NOT NULL,
  previous_json TEXT,
  resolution_json TEXT NOT NULL,
  rationale TEXT,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(media_id, field, revision)
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  library_id TEXT NOT NULL REFERENCES libraries(id),
  revision INTEGER NOT NULL DEFAULT 1,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  document_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_revisions (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  document_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(project_id, revision)
);
CREATE TABLE IF NOT EXISTS command_records (
  command_id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  payload_digest TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  project_id TEXT REFERENCES projects(id),
  library_id TEXT REFERENCES libraries(id),
  status TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0,
  message TEXT,
  checkpoint_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at DESC);
CREATE TABLE IF NOT EXISTS job_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,
  progress REAL,
  message TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_events_job_sequence ON job_events(job_id, sequence);
CREATE TABLE IF NOT EXISTS suggestions (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
  library_id TEXT REFERENCES libraries(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  algorithm TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  config_digest TEXT NOT NULL,
  project_revision INTEGER,
  confidence REAL,
  suggestion_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS render_plans (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  project_revision INTEGER NOT NULL,
  plan_digest TEXT NOT NULL UNIQUE,
  source_set_digest TEXT NOT NULL,
  provenance_revision INTEGER NOT NULL,
  status TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_attestations (
  id TEXT PRIMARY KEY,
  render_plan_id TEXT NOT NULL REFERENCES render_plans(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  project_revision INTEGER NOT NULL,
  plan_digest TEXT NOT NULL,
  source_set_digest TEXT NOT NULL,
  provenance_revision INTEGER NOT NULL,
  warnings_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  render_plan_id TEXT NOT NULL REFERENCES render_plans(id),
  job_id TEXT REFERENCES jobs(id),
  output_grant_id TEXT NOT NULL REFERENCES directory_grants(id),
  filename TEXT NOT NULL,
  manifest_filename TEXT NOT NULL,
  status TEXT NOT NULL,
  video_digest TEXT,
  manifest_digest TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS render_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  status TEXT NOT NULL,
  output_path TEXT,
  progress REAL NOT NULL DEFAULT 0,
  message TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cache_entries (
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0,
  last_accessed TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3's context manager, then always close."""

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._event_condition = threading.Condition()
        self._event_generation = 0
        self._backup_before_migration()
        with self.connect() as db:
            db.executescript(SCHEMA)
            self._ensure_legacy_columns(db)
            db.execute(
                "INSERT OR IGNORE INTO project_revisions(project_id,revision,document_json,created_at) "
                "SELECT id,revision,document_json,updated_at FROM projects"
            )
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at,details_json) VALUES(?,?,?)",
                (SCHEMA_VERSION, now_iso(), json.dumps({"name": "canonical-v1"})),
            )
        self.interrupt_orphaned_jobs()
        self.compact_events()
        self.prune_cache()

    def _backup_before_migration(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        source = sqlite3.connect(self.path)
        try:
            version = int(source.execute("PRAGMA user_version").fetchone()[0])
            if version >= SCHEMA_VERSION:
                return
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup = self.path.with_name(f"{self.path.name}.backup-v{version}-{timestamp}")
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
                if str(destination.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                    raise RuntimeError("Pre-migration backup failed integrity verification")
            finally:
                destination.close()
        finally:
            source.close()
        staging = self.path.with_name(f".{self.path.name}.migration-{timestamp}")
        source = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        destination = sqlite3.connect(staging)
        try:
            source.backup(destination)
            destination.executescript(SCHEMA)
            self._ensure_legacy_columns(destination)
            destination.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            destination.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at,details_json) VALUES(?,?,?)",
                (SCHEMA_VERSION, now_iso(), json.dumps({"name": "canonical-v1", "atomicStaging": True})),
            )
            destination.commit()
            if str(destination.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError("Staged migration failed integrity verification")
        except Exception:
            destination.close()
            source.close()
            for candidate in (staging, Path(f"{staging}-wal"), Path(f"{staging}-shm")):
                candidate.unlink(missing_ok=True)
            raise
        else:
            destination.close()
            source.close()
        os.replace(staging, self.path)
        for suffix in ("-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            Path(f"{staging}{suffix}").unlink(missing_ok=True)

    def _ensure_legacy_columns(self, db: sqlite3.Connection) -> None:
        additions = {
            "libraries": {
                "grant_id": "TEXT REFERENCES directory_grants(id)",
                "time_zone": "TEXT NOT NULL DEFAULT 'UTC'",
                "dst_fold": "INTEGER NOT NULL DEFAULT 0",
                "nonexistent_policy": "TEXT NOT NULL DEFAULT 'REJECT'",
                "current_generation": "INTEGER NOT NULL DEFAULT 0",
            },
            "media": {
                "first_generation": "INTEGER NOT NULL DEFAULT 0",
                "last_generation": "INTEGER NOT NULL DEFAULT 0",
                "missing": "INTEGER NOT NULL DEFAULT 0",
                "fingerprint_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "projects": {
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in additions.items():
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
            factory=ClosingConnection,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    # Directory grants and libraries

    def create_grant(self, root: Path, role: str) -> dict[str, Any]:
        if role not in {"READ_ONLY_SOURCE", "WRITE_OUTPUT"}:
            raise DomainError("VALIDATION_FAILED", "Unknown directory grant role")
        resolved = root.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise DomainError("VALIDATION_FAILED", "Directory grant requires an existing directory")
        with self._lock, self.connect() as db:
            for row in db.execute("SELECT root,role FROM directory_grants WHERE revoked=0"):
                other = Path(row["root"])
                if role != row["role"] and (_contains(other, resolved) or _contains(resolved, other)):
                    raise DomainError("FORBIDDEN", "Source and output grants may not overlap")
            existing = db.execute(
                "SELECT * FROM directory_grants WHERE root=? AND role=? AND revoked=0", (str(resolved), role)
            ).fetchone()
            if existing:
                return self._public_grant(existing)
            grant_id = opaque_id("grant")
            db.execute(
                "INSERT INTO directory_grants(id,role,root,created_at) VALUES(?,?,?,?)",
                (grant_id, role, str(resolved), now_iso()),
            )
            row = db.execute("SELECT * FROM directory_grants WHERE id=?", (grant_id,)).fetchone()
            return self._public_grant(row)

    def grants(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [self._public_grant(row) for row in db.execute("SELECT * FROM directory_grants ORDER BY created_at")]

    def grant(self, grant_id: str, role: str | None = None) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM directory_grants WHERE id=?", (grant_id,)).fetchone()
            if not row or row["revoked"]:
                raise DomainError("GRANT_REQUIRED", "Directory grant is unavailable")
            if role and row["role"] != role:
                raise DomainError("FORBIDDEN", f"Directory grant must have role {role}")
            return dict(row)

    def revoke_grant(self, grant_id: str) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM directory_grants WHERE id=?", (grant_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Directory grant not found")
            db.execute("UPDATE directory_grants SET revoked=1,revoked_at=? WHERE id=?", (now_iso(), grant_id))
            dependent_jobs = list(
                db.execute(
                    "SELECT DISTINCT jobs.id FROM jobs LEFT JOIN projects ON projects.id=jobs.project_id "
                    "LEFT JOIN artifacts ON artifacts.job_id=jobs.id "
                    "WHERE jobs.status IN ('QUEUED','RUNNING','CANCEL_REQUESTED') AND ("
                    "jobs.library_id IN (SELECT id FROM libraries WHERE grant_id=?) OR "
                    "projects.library_id IN (SELECT id FROM libraries WHERE grant_id=?) OR "
                    "artifacts.output_grant_id=?)",
                    (grant_id, grant_id, grant_id),
                )
            )
            for job in dependent_jobs:
                db.execute(
                    "UPDATE scan_generations SET cancel_requested=1,status='CANCEL_REQUESTED',message=?,updated_at=? "
                    "WHERE id=? AND status IN ('QUEUED','RUNNING','CANCEL_REQUESTED')",
                    ("Directory grant revoked", now_iso(), job["id"]),
                )
                self._transition_job_db(
                    db,
                    job["id"],
                    "CANCEL_REQUESTED",
                    None,
                    "Stopping work because its directory grant was revoked",
                    {"reason": "GRANT_REQUIRED"},
                    error_code="GRANT_REQUIRED",
                )
            return self._public_grant(db.execute("SELECT * FROM directory_grants WHERE id=?", (grant_id,)).fetchone())

    @staticmethod
    def _public_grant(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "role": row["role"],
            "label": Path(row["root"]).name or "Selected directory",
            "revoked": bool(row["revoked"]),
            "createdAt": row["created_at"],
            "revokedAt": row["revoked_at"],
        }

    def create_library(
        self,
        grant_id: str,
        time_zone: str = "UTC",
        dst_fold: int = 0,
        nonexistent_policy: str = "REJECT",
    ) -> dict[str, Any]:
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise DomainError("VALIDATION_FAILED", "Library timeZone must be a valid IANA zone") from error
        if nonexistent_policy not in {"REJECT", "SHIFT_FORWARD"}:
            raise DomainError("VALIDATION_FAILED", "Unknown nonexistent local-time policy")
        grant = self.grant(grant_id, "READ_ONLY_SOURCE")
        library_id = f"lib_{digest_json(str(grant['root']))[:24]}"
        with self._lock, self.connect() as db:
            previous = db.execute(
                "SELECT time_zone,dst_fold,nonexistent_policy FROM libraries WHERE id=?", (library_id,)
            ).fetchone()
            db.execute(
                "INSERT INTO libraries(id,grant_id,root,time_zone,dst_fold,nonexistent_policy,summary_json) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET grant_id=excluded.grant_id,time_zone=excluded.time_zone,"
                "dst_fold=excluded.dst_fold,nonexistent_policy=excluded.nonexistent_policy",
                (library_id, grant_id, grant["root"], time_zone, int(bool(dst_fold)), nonexistent_policy, "{}"),
            )
            changed = previous and (
                previous["time_zone"] != time_zone
                or int(previous["dst_fold"]) != int(bool(dst_fold))
                or previous["nonexistent_policy"] != nonexistent_policy
            )
            if changed:
                self._renormalize_library_timestamps_db(
                    db, library_id, time_zone, int(bool(dst_fold)), nonexistent_policy
                )
            if changed:
                self._invalidate_suggestions_db(
                    db,
                    "library_id=? AND status IN ('PENDING','ACCEPTED')",
                    (library_id,),
                    "Library timestamp policy changed",
                )
        return self.library(library_id)

    def update_library_time_policy(
        self,
        library_id: str,
        time_zone: str,
        dst_fold: int = 0,
        nonexistent_policy: str = "REJECT",
    ) -> dict[str, Any]:
        library = self.library(library_id)
        return self.create_library(
            library["sourceGrantId"], time_zone, dst_fold, nonexistent_policy
        )

    def library(self, library_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Library not found")
            return self._public_library(row)

    def libraries(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                self._public_library(row)
                for row in db.execute("SELECT * FROM libraries ORDER BY COALESCE(last_scan,'') DESC,id")
            ]

    @staticmethod
    def _public_library(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "sourceGrantId": row["grant_id"],
            "label": Path(row["root"]).name or "Video library",
            "timeZone": row["time_zone"],
            "dstFold": int(row["dst_fold"]),
            "nonexistentPolicy": row["nonexistent_policy"],
            "currentGeneration": int(row["current_generation"]),
            "lastScan": row["last_scan"],
            "summary": json.loads(row["summary_json"] or "{}"),
        }

    def library_root(self, library_id: str) -> Path:
        with self.connect() as db:
            row = db.execute(
                "SELECT l.root,l.grant_id,g.revoked FROM libraries l "
                "LEFT JOIN directory_grants g ON g.id=l.grant_id WHERE l.id=?",
                (library_id,),
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Unknown library")
            if row["grant_id"] is None or row["revoked"] is None or row["revoked"]:
                raise DomainError("GRANT_REQUIRED", "Library source grant has been revoked")
            return Path(row["root"])

    # Scans and media

    def begin_scan(self, library_id: str, mode: str, limit: int | None = None) -> dict[str, Any]:
        if mode not in {"FULL", "INCREMENTAL", "BOUNDED"}:
            raise DomainError("VALIDATION_FAILED", "Unknown scan mode")
        self.library_root(library_id)
        with self._lock, self.connect() as db:
            active = db.execute(
                "SELECT id FROM scan_generations WHERE library_id=? AND status IN ('QUEUED','RUNNING','CANCEL_REQUESTED')",
                (library_id,),
            ).fetchone()
            if active:
                raise DomainError("JOB_STATE_CONFLICT", "A scan is already active for this library")
            generation = int(
                db.execute(
                    "SELECT MAX(l.current_generation,COALESCE(MAX(s.generation),0))+1 "
                    "FROM libraries l LEFT JOIN scan_generations s ON s.library_id=l.id WHERE l.id=?",
                    (library_id,),
                ).fetchone()[0]
            )
            scan_id = opaque_id("scan")
            timestamp = now_iso()
            db.execute(
                "INSERT INTO scan_generations(id,library_id,generation,mode,status,limit_count,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (scan_id, library_id, generation, mode, "QUEUED", limit, timestamp, timestamp),
            )
            self._create_job_db(db, scan_id, "SCAN", library_id=library_id, message="Scan queued")
            return self.scan(scan_id, db)

    def scan(self, scan_id: str, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        owns = db is None
        db = db or self.connect()
        try:
            row = db.execute("SELECT * FROM scan_generations WHERE id=?", (scan_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Scan not found")
            return {
                "id": row["id"],
                "libraryId": row["library_id"],
                "generation": int(row["generation"]),
                "mode": row["mode"],
                "status": row["status"],
                "limit": row["limit_count"],
                "scanned": int(row["scanned"]),
                "videos": int(row["videos"]),
                "warnings": int(row["warnings"]),
                "cancelRequested": bool(row["cancel_requested"]),
                "message": row["message"],
                "summary": json.loads(row["summary_json"] or "{}"),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
        finally:
            if owns:
                db.close()

    def scan_progress(
        self,
        scan_id: str,
        *,
        processed: int = 1,
        warning: bool = False,
        warning_count: int | None = None,
        message: str | None = None,
    ) -> None:
        if processed < 1:
            raise DomainError("VALIDATION_FAILED", "Scan progress must include at least one record")
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM scan_generations WHERE id=?", (scan_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Scan not found")
            if row["cancel_requested"] or row["status"] in {"CANCEL_REQUESTED", "CANCELED", "FAILED", "SUCCEEDED"}:
                return
            scanned = int(row["scanned"]) + processed
            warnings = int(row["warnings"]) + (int(warning) if warning_count is None else warning_count)
            db.execute(
                "UPDATE scan_generations SET status='RUNNING',scanned=?,videos=?,warnings=?,message=?,updated_at=? WHERE id=?",
                (scanned, scanned, warnings, message, now_iso(), scan_id),
            )
            self._transition_job_db(db, scan_id, "RUNNING", min(0.99, scanned / max(scanned + 1, 1)), message)

    def cancel_scan(self, scan_id: str) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT status FROM scan_generations WHERE id=?", (scan_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Scan not found")
            if row["status"] not in {"CANCELED", "SUCCEEDED", "FAILED"}:
                db.execute(
                    "UPDATE scan_generations SET cancel_requested=1,status='CANCEL_REQUESTED',updated_at=? WHERE id=?",
                    (now_iso(), scan_id),
                )
                self._transition_job_db(db, scan_id, "CANCEL_REQUESTED", None, "Cancellation requested")
            return self.scan(scan_id, db)

    def scan_cancel_requested(self, scan_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT cancel_requested FROM scan_generations WHERE id=?", (scan_id,)).fetchone()
            return bool(row and row[0])

    def save_media_batch(self, scan_id: str, records: Iterable[MediaRecord]) -> None:
        records = list(records)
        if not records:
            return
        with self._lock, self.connect() as db:
            scan = db.execute("SELECT library_id,generation FROM scan_generations WHERE id=?", (scan_id,)).fetchone()
            if not scan:
                raise DomainError("NOT_FOUND", "Scan not found")
            policy = db.execute(
                "SELECT root,time_zone,dst_fold,nonexistent_policy FROM libraries WHERE id=?",
                (scan["library_id"],),
            ).fetchone()
            library_root = Path(policy["root"])
            for record in records:
                _normalize_media_record_timestamp(record, dict(policy))
                fingerprint = record.fingerprint or {}
                identity_material = {
                    "device": fingerprint.get("device"),
                    "inode": fingerprint.get("inode"),
                    "size": fingerprint.get("size"),
                    "sampleSha256": fingerprint.get("sampleSha256"),
                }
                identity_key = digest_json(identity_material) if all(
                    identity_material.get(key) is not None for key in ("device", "inode", "size", "sampleSha256")
                ) else None
                if identity_key and not db.execute("SELECT 1 FROM media WHERE id=?", (record.id,)).fetchone():
                    rename_candidates = list(
                        db.execute(
                            "SELECT keys.asset_id,media.relative_path FROM media_identity_keys keys "
                            "JOIN media ON media.id=keys.asset_id WHERE keys.library_id=? AND keys.identity_key=?",
                            (scan["library_id"], identity_key),
                        )
                    )
                    rename_matches = []
                    for item in rename_candidates:
                        previous_path = library_root / item["relative_path"]
                        try:
                            previous_identity = _identity_key_from_fingerprint(quick_fingerprint(previous_path))
                        except OSError:
                            previous_identity = None
                        if previous_identity != identity_key:
                            rename_matches.append(item)
                    if len(rename_matches) == 1:
                        record.id = rename_matches[0]["asset_id"]
                target_owner = db.execute(
                    "SELECT media.id,keys.identity_key FROM media LEFT JOIN media_identity_keys keys "
                    "ON keys.asset_id=media.id WHERE media.library_id=? AND media.relative_path=?",
                    (scan["library_id"], record.relative_path),
                ).fetchone()
                if target_owner and identity_key and target_owner["identity_key"] == identity_key:
                    record.id = target_owner["id"]
                existing_asset = db.execute(
                    "SELECT id,relative_path,record_json FROM media "
                    "WHERE library_id=? AND relative_path=? AND id<>?",
                    (scan["library_id"], record.relative_path, record.id),
                ).fetchone()
                if existing_asset:
                    moving = db.execute(
                        "SELECT relative_path FROM media WHERE id=?", (record.id,)
                    ).fetchone()
                    replacement_path = f".__unavailable__/{existing_asset['id']}"
                    replacement_missing = 1
                    if moving:
                        old_path = library_root / moving["relative_path"]
                        try:
                            old_identity = _identity_key_from_fingerprint(quick_fingerprint(old_path))
                        except OSError:
                            old_identity = None
                        owner_identity_row = db.execute(
                            "SELECT identity_key FROM media_identity_keys WHERE asset_id=?",
                            (existing_asset["id"],),
                        ).fetchone()
                        if owner_identity_row and old_identity == owner_identity_row["identity_key"]:
                            replacement_path = moving["relative_path"]
                            replacement_missing = 0
                        db.execute(
                            "UPDATE media SET relative_path=? WHERE id=?",
                            (f".__moving__/{record.id}-{scan_id}", record.id),
                        )
                    owner_payload = json.loads(existing_asset["record_json"])
                    owner_payload["relative_path"] = replacement_path
                    owner_payload["missing"] = bool(replacement_missing)
                    db.execute(
                        "UPDATE media SET relative_path=?,missing=?,record_json=? WHERE id=?",
                        (replacement_path, replacement_missing, json.dumps(owner_payload), existing_asset["id"]),
                    )
                payload = record.to_dict()
                payload["generation"] = int(scan["generation"])
                payload["missing"] = False
                db.execute(
                    "INSERT INTO media(id,library_id,relative_path,captured_at,camera,duration,first_generation,last_generation,missing,fingerprint_json,record_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET relative_path=excluded.relative_path,"
                    "captured_at=excluded.captured_at,camera=excluded.camera,duration=excluded.duration,last_generation=excluded.last_generation,"
                    "missing=0,fingerprint_json=excluded.fingerprint_json,record_json=excluded.record_json",
                    (
                        record.id,
                        scan["library_id"],
                        record.relative_path,
                        record.captured_at,
                        record.camera,
                        record.duration,
                        scan["generation"],
                        scan["generation"],
                        0,
                        json.dumps(record.fingerprint),
                        json.dumps(payload),
                    ),
                )
                if identity_key:
                    db.execute(
                        "INSERT INTO media_identity_keys(asset_id,library_id,identity_key) VALUES(?,?,?) "
                        "ON CONFLICT(asset_id) DO UPDATE SET library_id=excluded.library_id,identity_key=excluded.identity_key",
                        (record.id, scan["library_id"], identity_key),
                    )

    def _renormalize_library_timestamps_db(
        self,
        db: sqlite3.Connection,
        library_id: str,
        time_zone: str,
        dst_fold: int,
        nonexistent_policy: str,
    ) -> None:
        cursor = db.execute("SELECT id,record_json FROM media WHERE library_id=? ORDER BY id", (library_id,))
        while rows := cursor.fetchmany(500):
            for row in rows:
                self._renormalize_timestamp_row_db(
                    db, row, library_id, time_zone, dst_fold, nonexistent_policy
                )

    def _renormalize_timestamp_row_db(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        library_id: str,
        time_zone: str,
        dst_fold: int,
        nonexistent_policy: str,
    ) -> None:
        record = media_record_from_dict(json.loads(row["record_json"]), library_id)
        custom_policy = record.custom.get("timestampPolicy", {}) if isinstance(record.custom, dict) else {}
        raw = custom_policy.get("rawValue") or _raw_timestamp_from_evidence(record)
        if raw is None:
            return
        record.captured_at = str(raw)
        _normalize_media_record_timestamp(
            record,
            {
                "time_zone": time_zone,
                "dst_fold": dst_fold,
                "nonexistent_policy": nonexistent_policy,
            },
        )
        db.execute(
            "UPDATE media SET captured_at=?,record_json=? WHERE id=?",
            (record.captured_at, json.dumps(record.to_dict()), row["id"]),
        )

    def finish_scan(
        self,
        scan_id: str,
        status: str,
        summary: dict[str, Any],
        message: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in {"SUCCEEDED", "FAILED", "CANCELED"}:
            raise DomainError("VALIDATION_FAILED", "Invalid terminal scan state")
        with self._lock, self.connect() as db:
            scan = db.execute("SELECT * FROM scan_generations WHERE id=?", (scan_id,)).fetchone()
            if not scan:
                raise DomainError("NOT_FOUND", "Scan not found")
            if scan["cancel_requested"]:
                job = db.execute("SELECT error_code FROM jobs WHERE id=?", (scan_id,)).fetchone()
                error_code = (job["error_code"] if job else None) or error_code
                grant_revoked = error_code == "GRANT_REQUIRED"
                status = "FAILED" if grant_revoked else "CANCELED"
                error_code = "GRANT_REQUIRED" if grant_revoked else None
                message = (
                    "Directory grant revoked; visited records retained"
                    if grant_revoked
                    else "Scan canceled; visited records retained"
                )
                summary = {**summary, "interrupted": True}
            if status == "SUCCEEDED" and scan["mode"] == "FULL":
                db.execute(
                    "UPDATE media SET missing=1 WHERE library_id=? AND last_generation<?",
                    (scan["library_id"], scan["generation"]),
                )
            db.execute(
                "UPDATE scan_generations SET status=?,summary_json=?,message=?,updated_at=? WHERE id=?",
                (status, json.dumps(summary), message, now_iso(), scan_id),
            )
            if status == "SUCCEEDED":
                db.execute(
                    "UPDATE libraries SET current_generation=?,last_scan=?,summary_json=? WHERE id=?",
                    (scan["generation"], now_iso(), json.dumps(summary), scan["library_id"]),
                )
                self._invalidate_suggestions_db(
                    db,
                    "library_id=? AND status IN ('PENDING','ACCEPTED')",
                    (scan["library_id"],),
                    "A newer scan generation may have changed the suggestion inputs",
                )
            self._transition_job_db(
                db,
                scan_id,
                status,
                1 if status == "SUCCEEDED" else 0,
                message,
                error_code=error_code,
            )

    def existing_media_by_path(self, library_id: str, relative_path: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT record_json FROM media WHERE library_id=? AND relative_path=?", (library_id, relative_path)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def media_page(
        self, library_id: str, limit: int = 200, cursor: str | None = None, generation: int | None = None
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        library = self.library(library_id)
        snapshot = int(generation if generation is not None else library["currentGeneration"])
        after_path, after_id = "", ""
        if cursor:
            try:
                padding = "=" * (-len(cursor) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
                if int(decoded["generation"]) != snapshot:
                    raise ValueError
                after_path, after_id = str(decoded["path"]), str(decoded["id"])
            except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
                raise DomainError("VALIDATION_FAILED", "Invalid or stale media cursor") from error
        with self.connect() as db:
            rows = list(
                db.execute(
                    "SELECT id,relative_path,record_json,missing FROM media WHERE library_id=? AND first_generation<=? "
                    "AND (relative_path>? OR (relative_path=? AND id>?)) ORDER BY relative_path,id LIMIT ?",
                    (library_id, snapshot, after_path, after_path, after_id, limit + 1),
                )
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            items = []
            for row in rows:
                item = json.loads(row["record_json"])
                item["missing"] = bool(row["missing"])
                items.append(item)
            next_cursor = None
            if has_more and rows:
                raw = json.dumps(
                    {"generation": snapshot, "path": rows[-1]["relative_path"], "id": rows[-1]["id"]},
                    separators=(",", ":"),
                ).encode("utf-8")
                next_cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            total = int(
                db.execute(
                    "SELECT COUNT(*) FROM media WHERE library_id=? AND first_generation<=?", (library_id, snapshot)
                ).fetchone()[0]
            )
            return {"items": items, "nextCursor": next_cursor, "snapshotGeneration": snapshot, "total": total}

    def media(self, library_id: str, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT record_json,missing FROM media WHERE library_id=? ORDER BY relative_path,id LIMIT ? OFFSET ?",
                (library_id, limit, offset),
            )
            result = []
            for row in rows:
                item = json.loads(row["record_json"])
                item["missing"] = bool(row["missing"])
                result.append(item)
            return result

    def media_record(self, media_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT record_json,missing FROM media WHERE id=?", (media_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", f"Unknown media: {media_id}")
            item = json.loads(row["record_json"])
            item["missing"] = bool(row["missing"])
            return item

    def media_records(self, media_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(media_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            result: dict[str, dict[str, Any]] = {}
            for row in db.execute(f"SELECT id,record_json,missing FROM media WHERE id IN ({placeholders})", ids):
                item = json.loads(row["record_json"])
                item["missing"] = bool(row["missing"])
                result[row["id"]] = item
            return result

    def save_scan(self, summary: ScanSummary, records: list[MediaRecord]) -> None:
        """Compatibility seam for legacy tests and imports."""
        root = Path(summary.root).resolve(strict=True)
        source_grant = self.create_grant(root, "READ_ONLY_SOURCE")
        library = self.create_library(source_grant["id"])
        scan = self.begin_scan(library["id"], "FULL")
        for record in records:
            record.library_id = library["id"]
        self.save_media_batch(scan["id"], records)
        self.finish_scan(scan["id"], "SUCCEEDED", summary.to_dict())
        if summary.library_id != library["id"]:
            with self._lock, self.connect() as db:
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("UPDATE libraries SET id=? WHERE id=?", (summary.library_id, library["id"]))
                db.execute("UPDATE media SET library_id=? WHERE library_id=?", (summary.library_id, library["id"]))
                db.execute("UPDATE scan_generations SET library_id=? WHERE library_id=?", (summary.library_id, library["id"]))
                db.execute("UPDATE jobs SET library_id=? WHERE library_id=?", (summary.library_id, library["id"]))
                db.execute("PRAGMA foreign_keys=ON")

    # Projects and commands

    def create_project(self, name: str, library_id: str, asset_ids: list[str]) -> dict[str, Any]:
        self.library_root(library_id)
        assets = self.media_records(asset_ids)
        if len(assets) != len(set(asset_ids)):
            raise DomainError("NOT_FOUND", "One or more selected media assets are unavailable")
        if any(asset.get("library_id") != library_id for asset in assets.values()):
            raise DomainError("VALIDATION_FAILED", "Every selected media asset must belong to the project library")
        project = new_project(name, library_id, [assets[item] for item in asset_ids])
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO projects(id,name,library_id,revision,archived,created_at,updated_at,document_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    project["id"],
                    project["name"],
                    project["libraryId"],
                    project["revision"],
                    0,
                    project["createdAt"],
                    project["updatedAt"],
                    json.dumps(project),
                ),
            )
            db.execute(
                "INSERT INTO project_revisions(project_id,revision,document_json,created_at) VALUES(?,?,?,?)",
                (project["id"], project["revision"], json.dumps(project), project["createdAt"]),
            )
        return self.project(project["id"])

    def save_project(self, project: dict[str, Any]) -> None:
        canonical = self._migrate_legacy_project(project)
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO projects(id,name,library_id,revision,archived,created_at,updated_at,document_json) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,library_id=excluded.library_id,"
                "revision=excluded.revision,archived=excluded.archived,updated_at=excluded.updated_at,document_json=excluded.document_json",
                (
                    canonical["id"],
                    canonical["name"],
                    canonical["libraryId"],
                    canonical["revision"],
                    int(canonical.get("archived", False)),
                    canonical.get("createdAt", now_iso()),
                    canonical.get("updatedAt", now_iso()),
                    json.dumps(canonical),
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO project_revisions(project_id,revision,document_json,created_at) VALUES(?,?,?,?)",
                (canonical["id"], canonical["revision"], json.dumps(canonical), canonical.get("updatedAt", now_iso())),
            )

    def project(self, project_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT document_json FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Project not found")
            return self._migrate_legacy_project(json.loads(row[0]))

    def projects(self, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            where = "" if include_archived else "WHERE archived=0"
            return [
                self._migrate_legacy_project(json.loads(row[0]))
                for row in db.execute(f"SELECT document_json FROM projects {where} ORDER BY updated_at DESC")
            ]

    def project_revision(self, project_id: str, revision: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT document_json FROM project_revisions WHERE project_id=? AND revision=?",
                (project_id, int(revision)),
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Retained project revision not found")
            return self._migrate_legacy_project(json.loads(row[0]))

    def apply_project_command(self, project_id: str, envelope: dict[str, Any], preview: bool = False) -> dict[str, Any]:
        command_id = str(envelope.get("commandId", ""))
        if not command_id:
            raise DomainError("VALIDATION_FAILED", "commandId is required")
        try:
            expected_revision = int(envelope["expectedRevision"])
            command_type = str(envelope["commandType"])
            payload = dict(envelope.get("payload") or {})
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError("VALIDATION_FAILED", "Invalid project command envelope") from error
        payload_digest = digest_json(
            {"commandType": command_type, "payload": payload, "expectedRevision": expected_revision}
        )
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT project_id,payload_digest,result_json FROM command_records WHERE command_id=?", (command_id,)
            ).fetchone()
            if previous:
                if previous["project_id"] != project_id or previous["payload_digest"] != payload_digest:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "commandId was already used with different content")
                return json.loads(previous["result_json"])
            row = db.execute("SELECT document_json,revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Project not found")
            project = self._migrate_legacy_project(json.loads(row["document_json"]))
            current_revision = int(row["revision"])
            if expected_revision != current_revision:
                raise DomainError(
                    "REVISION_CONFLICT",
                    "Project changed since this command was prepared",
                    {"currentRevision": current_revision, "project": project},
                )
            assets = self._media_records_db(db, [item["assetId"] for item in project.get("clips", [])])
            previous_compiled = compile_program(project, assets) if command_type in PROGRAM_AFFECTING_COMMANDS else None
            changed = apply_command(project, command_type, payload, assets)
            changed["revision"] = current_revision + 1
            changed["updatedAt"] = now_iso()
            compiled = compile_program(changed, assets)
            result = {
                "commandId": command_id,
                "projectId": project_id,
                "previousRevision": current_revision,
                "appliedRevision": changed["revision"],
                "project": changed,
                "issues": compiled["issues"],
                "reviewState": "STALE" if project.get("review") else "NOT_REVIEWED",
                "eventCursor": self.latest_event_sequence(db),
                "preview": preview,
                "affectedIntervals": _affected_program_intervals(previous_compiled, compiled) if previous_compiled else [],
            }
            if preview:
                db.rollback()
                return result
            db.execute(
                "UPDATE projects SET name=?,revision=?,archived=?,updated_at=?,document_json=? WHERE id=?",
                (
                    changed["name"],
                    changed["revision"],
                    int(changed.get("archived", False)),
                    changed["updatedAt"],
                    json.dumps(changed),
                    project_id,
                ),
            )
            db.execute(
                "INSERT INTO project_revisions(project_id,revision,document_json,created_at) VALUES(?,?,?,?)",
                (project_id, changed["revision"], json.dumps(changed), changed["updatedAt"]),
            )
            db.execute(
                "INSERT INTO command_records(command_id,project_id,payload_digest,result_json,created_at) VALUES(?,?,?,?,?)",
                (command_id, project_id, payload_digest, json.dumps(result), now_iso()),
            )
            self._invalidate_suggestions_db(
                db,
                "project_id=? AND project_revision<? AND status='PENDING'",
                (project_id, changed["revision"]),
                "Project revision changed after suggestion creation",
            )
            if command_type in {"AcceptAlignmentSuggestion", "RejectAlignmentSuggestion"}:
                suggestion_id = str(payload.get("suggestionId", ""))
                next_status = "ACCEPTED" if command_type == "AcceptAlignmentSuggestion" else "REJECTED"
                self._set_suggestion_status_db(db, suggestion_id, next_status)
            return result

    def compiled_project(self, project_id: str) -> dict[str, Any]:
        project = self.project(project_id)
        assets = self.media_records(item["assetId"] for item in project["clips"])
        return compile_program(project, assets)

    def _migrate_legacy_project(self, project: dict[str, Any]) -> dict[str, Any]:
        if "logicalSources" in project:
            return project
        media_ids = list((project.get("alignment") or {}).keys())
        media_ids += [
            item["mediaId"]
            for item in project.get("videoSegments", []) + project.get("audioSegments", [])
            if item.get("mediaId")
        ]
        assets = self.media_records(media_ids)
        if not assets:
            return project
        canonical = new_project(
            project.get("name", "Migrated project"), project["libraryId"], list(assets.values()), project["id"]
        )
        source_remap: dict[str, str] = {}
        clip_remap: dict[str, str] = {}
        for source, clip in zip(canonical["logicalSources"], canonical["clips"]):
            asset_id = str(clip["assetId"])
            next_source_id = _stable_migration_id("src", project["id"], asset_id)
            next_clip_id = _stable_migration_id("clip", project["id"], asset_id)
            source_remap[source["id"]] = next_source_id
            clip_remap[clip["id"]] = next_clip_id
            source["id"] = next_source_id
            clip["id"] = next_clip_id
            clip["logicalSourceId"] = next_source_id
        for index, block in enumerate(canonical["videoBlocks"]):
            block["id"] = _stable_migration_id("vblock", project["id"], index, block["startUs"], block["endUs"])
            block["logicalSourceId"] = source_remap.get(block["logicalSourceId"], block["logicalSourceId"])
            if block.get("pinnedClipId"):
                block["pinnedClipId"] = clip_remap.get(block["pinnedClipId"], block["pinnedClipId"])
        for index, block in enumerate(canonical["audioBlocks"]):
            block["id"] = _stable_migration_id("ablock", project["id"], index, block["startUs"], block["endUs"])
        canonical["revision"] = int(project.get("revision", 1))
        canonical["legacy"] = copy.deepcopy(project)
        canonical["migration"] = {
            "version": 1,
            "sourceTimeUnit": "floating-seconds",
            "targetTimeUnit": "integer-microseconds",
            "rounding": "half-even",
            "reviewInvalidated": True,
        }
        canonical["anchorMode"] = (
            "SOURCE_TIME" if project.get("cutAnchoring") == "source-clips" else "PROGRAM_TIME"
        )
        clip_by_asset = {item["assetId"]: item for item in canonical["clips"]}
        source_by_asset = {item["assetId"]: item["logicalSourceId"] for item in canonical["clips"]}
        for media_id, alignment in (project.get("alignment") or {}).items():
            clip = clip_by_asset.get(media_id)
            if clip:
                clip["sync"]["anchorOutputUs"] = int(alignment.get("offsetMs", 0)) * 1_000
        if project.get("videoSegments"):
            canonical["videoBlocks"] = [
                {
                    "id": item.get("id") or _stable_migration_id(
                        "vblock", project["id"], index, item["start"], item["end"], item.get("mediaId")
                    ),
                    "startUs": seconds_to_us(item["start"]),
                    "endUs": seconds_to_us(item["end"]),
                    "logicalSourceId": source_by_asset[item["mediaId"]],
                    "pinnedClipId": clip_by_asset[item["mediaId"]]["id"],
                }
                for index, item in enumerate(project["videoSegments"])
                if item.get("mediaId") in source_by_asset
            ]
        if project.get("audioSegments"):
            canonical["audioBlocks"] = []
            for index, item in enumerate(project["audioSegments"]):
                media_id = item.get("mediaId")
                explicit_silence = bool(item.get("silence")) or item.get("provenance", {}).get("source") == "silence"
                if not media_id and not explicit_silence:
                    continue
                mode = "FOLLOW_VIDEO" if item.get("linked", True) and media_id else ("FIXED_CLIP" if media_id else "SILENCE")
                canonical["audioBlocks"].append(
                    {
                        "id": item.get("id") or _stable_migration_id(
                            "ablock", project["id"], index, item["start"], item["end"], media_id
                        ),
                        "startUs": seconds_to_us(item["start"]),
                        "endUs": seconds_to_us(item["end"]),
                        "mode": mode,
                        "logicalSourceId": source_by_asset.get(media_id),
                        "clipId": clip_by_asset.get(media_id, {}).get("id"),
                        "offsetUs": int(item.get("offsetMs", 0)) * 1_000,
                        "ratePpm": 0,
                    }
                )
        canonical["review"] = None
        return canonical

    def _media_records_db(self, db: sqlite3.Connection, media_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(media_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        result = {}
        for row in db.execute(f"SELECT id,record_json,missing FROM media WHERE id IN ({placeholders})", ids):
            item = json.loads(row["record_json"])
            item["missing"] = bool(row["missing"])
            result[row["id"]] = item
        return result

    # Provenance resolutions and suggestions

    def resolve_provenance(
        self, media_id: str, field: str, resolution: dict[str, Any], rationale: str | None, actor: str
    ) -> dict[str, Any]:
        self.media_record(media_id)
        with self._lock, self.connect() as db:
            previous = db.execute(
                "SELECT revision,resolution_json FROM provenance_resolutions WHERE media_id=? AND field=? "
                "ORDER BY revision DESC LIMIT 1",
                (media_id, field),
            ).fetchone()
            revision = int(previous["revision"] if previous else 0) + 1
            resolution_id = opaque_id("resolution")
            timestamp = now_iso()
            db.execute(
                "INSERT INTO provenance_resolutions(id,media_id,field,revision,previous_json,resolution_json,rationale,actor,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    resolution_id,
                    media_id,
                    field,
                    revision,
                    previous["resolution_json"] if previous else None,
                    json.dumps(resolution),
                    rationale,
                    actor,
                    timestamp,
                ),
            )
            projects = list(db.execute("SELECT id,document_json FROM projects"))
            for project_row in projects:
                project = self._migrate_legacy_project(json.loads(project_row["document_json"]))
                if not any(clip.get("assetId") == media_id for clip in project.get("clips", [])):
                    continue
                project["provenanceRevision"] = int(project.get("provenanceRevision", 0)) + 1
                project["review"] = None
                project["updatedAt"] = now_iso()
                db.execute(
                    "UPDATE projects SET document_json=?,updated_at=? WHERE id=?",
                    (json.dumps(project), project["updatedAt"], project["id"]),
                )
                self._invalidate_suggestions_db(
                    db,
                    "project_id=? AND status IN ('PENDING','ACCEPTED')",
                    (project["id"],),
                    "A provenance resolution changed after suggestion creation",
                )
            result = {
                "id": resolution_id,
                "mediaId": media_id,
                "field": field,
                "revision": revision,
                "previous": json.loads(previous["resolution_json"]) if previous else None,
                "resolution": resolution,
                "rationale": rationale,
                "actor": actor,
                "createdAt": timestamp,
            }
        return result

    def provenance_resolutions(self, media_id: str, field: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM provenance_resolutions WHERE media_id=? "
                + ("AND field=? " if field else "")
                + "ORDER BY field,revision",
                (media_id, field) if field else (media_id,),
            )
            return [
                {
                    "id": row["id"],
                    "mediaId": row["media_id"],
                    "field": row["field"],
                    "revision": row["revision"],
                    "previous": json.loads(row["previous_json"]) if row["previous_json"] else None,
                    "resolution": json.loads(row["resolution_json"]),
                    "rationale": row["rationale"],
                    "actor": row["actor"],
                    "createdAt": row["created_at"],
                }
                for row in rows
            ]

    def provenance_snapshot(self, media_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(media_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        with self.connect() as db:
            for row in db.execute(
                f"SELECT * FROM provenance_resolutions WHERE media_id IN ({placeholders}) "
                "ORDER BY media_id,field,revision",
                ids,
            ):
                latest[(row["media_id"], row["field"])] = {
                    "id": row["id"],
                    "mediaId": row["media_id"],
                    "field": row["field"],
                    "revision": int(row["revision"]),
                    "resolution": json.loads(row["resolution_json"]),
                    "createdAt": row["created_at"],
                }
        return [latest[key] for key in sorted(latest)]

    def save_suggestion(self, suggestion: dict[str, Any]) -> dict[str, Any]:
        timestamp = now_iso()
        value = {
            "id": suggestion.get("id") or opaque_id("suggestion"),
            "status": "PENDING",
            "algorithmVersion": "1",
            "configDigest": digest_json(suggestion.get("config", {})),
            **suggestion,
        }
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO suggestions(id,project_id,library_id,kind,status,input_digest,algorithm,algorithm_version,"
                "config_digest,project_revision,confidence,suggestion_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value["id"],
                    value.get("projectId"),
                    value.get("libraryId"),
                    value["kind"],
                    value["status"],
                    value["inputDigest"],
                    value["algorithm"],
                    value["algorithmVersion"],
                    value["configDigest"],
                    value.get("projectRevision"),
                    value.get("confidence"),
                    json.dumps(value),
                    timestamp,
                    timestamp,
                ),
            )
        return value

    def suggestions(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT suggestion_json FROM suggestions WHERE project_id=? ORDER BY created_at DESC", (project_id,)
                )
            ]

    def library_suggestions(self, library_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT suggestion_json FROM suggestions WHERE library_id=? AND kind='CLUSTER' "
                    "ORDER BY created_at DESC",
                    (library_id,),
                )
            ]

    def clustering_media(self, library_id: str) -> list[dict[str, Any]]:
        self.library(library_id)
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record_json FROM media WHERE library_id=? AND missing=0 AND captured_at IS NOT NULL "
                    "ORDER BY captured_at,id",
                    (library_id,),
                )
            ]

    def _set_suggestion_status_db(self, db: sqlite3.Connection, suggestion_id: str, status: str) -> None:
        row = db.execute("SELECT suggestion_json FROM suggestions WHERE id=?", (suggestion_id,)).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Suggestion not found")
        value = json.loads(row[0])
        value["status"] = status
        value["updatedAt"] = now_iso()
        db.execute(
            "UPDATE suggestions SET status=?,suggestion_json=?,updated_at=? WHERE id=?",
            (status, json.dumps(value), value["updatedAt"], suggestion_id),
        )

    def _invalidate_suggestions_db(
        self,
        db: sqlite3.Connection,
        predicate: str,
        params: tuple[Any, ...],
        reason: str,
    ) -> None:
        rows = list(db.execute(f"SELECT id,suggestion_json FROM suggestions WHERE {predicate}", params))
        for row in rows:
            value = json.loads(row["suggestion_json"])
            value["status"] = "STALE"
            value["invalidationReason"] = reason
            value["updatedAt"] = now_iso()
            db.execute(
                "UPDATE suggestions SET status='STALE',suggestion_json=?,updated_at=? WHERE id=?",
                (json.dumps(value), value["updatedAt"], row["id"]),
            )

    # Durable jobs and events

    def create_job(
        self,
        kind: str,
        *,
        project_id: str | None = None,
        library_id: str | None = None,
        message: str = "Queued",
        job_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            job_id = job_id or opaque_id("job")
            self._create_job_db(db, job_id, kind, project_id, library_id, message)
            return self.job(job_id, db)

    def _create_job_db(
        self,
        db: sqlite3.Connection,
        job_id: str,
        kind: str,
        project_id: str | None = None,
        library_id: str | None = None,
        message: str = "Queued",
    ) -> None:
        timestamp = now_iso()
        db.execute(
            "INSERT OR IGNORE INTO jobs(id,kind,project_id,library_id,status,progress,message,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, kind, project_id, library_id, "QUEUED", 0, message, timestamp, timestamp),
        )
        if db.execute("SELECT changes()").fetchone()[0]:
            self._event_db(db, job_id, "STATE", "QUEUED", 0, message, {})

    def transition_job(
        self,
        job_id: str,
        status: str,
        progress: float | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            self._transition_job_db(db, job_id, status, progress, message, details, result, error_code)
            return self.job(job_id, db)

    def finish_job_error(self, job_id: str, message: str, error_code: str) -> dict[str, Any]:
        """Atomically preserve cancellation or finish the current job as failed."""
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Job not found")
            if row["status"] in TERMINAL_JOB_STATES:
                return self.job(job_id, db)
            if row["status"] == "CANCEL_REQUESTED":
                if row["error_code"] == "GRANT_REQUIRED":
                    status = "FAILED"
                    final_message = "Directory grant was revoked"
                    final_error = "GRANT_REQUIRED"
                else:
                    status = "CANCELED"
                    final_message = "Analysis canceled"
                    final_error = None
            else:
                status = "FAILED"
                final_message = message
                final_error = error_code
            self._transition_job_db(
                db,
                job_id,
                status,
                float(row["progress"]),
                final_message,
                error_code=final_error,
            )
            return self.job(job_id, db)

    def _transition_job_db(
        self,
        db: sqlite3.Connection,
        job_id: str,
        status: str,
        progress: float | None,
        message: str | None,
        details: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        aliases = {"complete": "SUCCEEDED", "failed": "FAILED", "canceled": "CANCELED", "running": "RUNNING", "queued": "QUEUED"}
        status = aliases.get(status, status)
        if status not in JOB_STATES:
            raise DomainError("VALIDATION_FAILED", f"Unknown job status: {status}")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Job not found")
        if row["status"] in TERMINAL_JOB_STATES and status != row["status"]:
            raise DomainError("JOB_STATE_CONFLICT", "Terminal job cannot transition")
        if row["status"] == "CANCEL_REQUESTED" and status not in {"CANCELED", "FAILED", "FAILED_RECOVERABLE"}:
            raise DomainError("JOB_STATE_CONFLICT", "Cancellation can only finish as canceled or failed")
        next_progress = float(row["progress"] if progress is None else max(0, min(1, progress)))
        db.execute(
            "UPDATE jobs SET status=?,progress=?,message=?,result_json=?,error_code=?,updated_at=? WHERE id=?",
            (
                status,
                next_progress,
                message if message is not None else row["message"],
                json.dumps(result) if result is not None else row["result_json"],
                error_code if error_code is not None else row["error_code"],
                now_iso(),
                job_id,
            ),
        )
        self._event_db(db, job_id, "STATE", status, next_progress, message, details or {})

    def _event_db(
        self,
        db: sqlite3.Connection,
        job_id: str,
        event_type: str,
        status: str,
        progress: float | None,
        message: str | None,
        details: dict[str, Any],
    ) -> None:
        cursor = db.execute(
            "INSERT INTO job_events(job_id,event_type,status,progress,message,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, event_type, status, progress, message, json.dumps(details), now_iso()),
        )
        with self._event_condition:
            self._event_generation += 1
            self._event_condition.notify_all()
        if int(cursor.lastrowid or 0) % 1_000 == 0:
            self._compact_events_db(db)

    def compact_events(self, max_events: int = MAX_JOB_EVENTS) -> int:
        with self._lock, self.connect() as db:
            return self._compact_events_db(db, max_events)

    def _compact_events_db(self, db: sqlite3.Connection, max_events: int = MAX_JOB_EVENTS) -> int:
        max_events = max(1_000, min(int(max_events), MAX_JOB_EVENTS))
        cutoff = db.execute(
            "SELECT sequence FROM job_events ORDER BY sequence DESC LIMIT 1 OFFSET ?",
            (max_events - 1,),
        ).fetchone()
        if not cutoff:
            return 0
        cursor = db.execute("DELETE FROM job_events WHERE sequence<?", (int(cutoff[0]),))
        return max(0, int(cursor.rowcount))

    def prune_cache(
        self,
        max_entries: int = MAX_CACHE_ENTRIES,
        max_bytes: int = MAX_CACHE_BYTES,
    ) -> dict[str, int]:
        """Evict only registered, unpinned derived files beneath state/cache."""
        cache_root = (self.path.parent / "cache").resolve()
        removed_entries = 0
        removed_bytes = 0
        with self._lock, self.connect() as db:
            rows = list(db.execute("SELECT * FROM cache_entries ORDER BY pinned DESC,last_accessed DESC"))
            kept_entries = 0
            kept_bytes = 0
            for row in rows:
                size = max(0, int(row["size_bytes"]))
                keep = bool(row["pinned"]) or (
                    kept_entries < max_entries and kept_bytes + size <= max_bytes
                )
                if keep:
                    kept_entries += 1
                    kept_bytes += size
                    continue
                target = Path(row["path"]).resolve()
                if target.is_relative_to(cache_root) and target.is_file():
                    target.unlink(missing_ok=True)
                db.execute("DELETE FROM cache_entries WHERE key=?", (row["key"],))
                removed_entries += 1
                removed_bytes += size
        return {"removedEntries": removed_entries, "removedBytes": removed_bytes}

    def job(self, job_id: str, db: sqlite3.Connection | None = None) -> dict[str, Any]:
        owns = db is None
        db = db or self.connect()
        try:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                legacy = db.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()
                if legacy:
                    status = {
                        "queued": "QUEUED",
                        "running": "RUNNING",
                        "complete": "SUCCEEDED",
                        "succeeded": "SUCCEEDED",
                        "canceled": "CANCELED",
                        "failed": "FAILED",
                    }.get(str(legacy["status"]).lower(), str(legacy["status"]).upper())
                    return {
                        "id": legacy["id"],
                        "kind": "RENDER",
                        "projectId": legacy["project_id"],
                        "libraryId": None,
                        "status": status,
                        "progress": legacy["progress"],
                        "message": legacy["message"],
                        "checkpoint": {},
                        "result": {"outputPath": legacy["output_path"]} if legacy["output_path"] else None,
                        "errorCode": None,
                        "createdAt": legacy["created_at"],
                        "updatedAt": legacy["updated_at"],
                    }
                raise DomainError("NOT_FOUND", "Job not found")
            return {
                "id": row["id"],
                "kind": row["kind"],
                "projectId": row["project_id"],
                "libraryId": row["library_id"],
                "status": row["status"],
                "progress": row["progress"],
                "message": row["message"],
                "checkpoint": json.loads(row["checkpoint_json"] or "{}"),
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "errorCode": row["error_code"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
        finally:
            if owns:
                db.close()

    def events(self, after: int = 0, limit: int = 500, job_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1_000))
        with self.connect() as db:
            query = "SELECT * FROM job_events WHERE sequence>?"
            params: list[Any] = [after]
            if job_id:
                query += " AND job_id=?"
                params.append(job_id)
            query += " ORDER BY sequence LIMIT ?"
            params.append(limit)
            return [
                {
                    "sequence": row["sequence"],
                    "jobId": row["job_id"],
                    "eventType": row["event_type"],
                    "status": row["status"],
                    "progress": row["progress"],
                    "message": row["message"],
                    "details": json.loads(row["details_json"] or "{}"),
                    "createdAt": row["created_at"],
                }
                for row in db.execute(query, params)
            ]

    def wait_for_events(self, after: int, timeout: float, limit: int = 500) -> list[dict[str, Any]]:
        """Wait for durable events without polling the database while idle."""

        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._event_condition:
            observed_generation = self._event_generation
        while True:
            events = self.events(after, limit)
            if events:
                return events
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            with self._event_condition:
                if observed_generation == self._event_generation:
                    self._event_condition.wait(remaining)
                else:
                    # A writer may have notified immediately before its SQLite
                    # transaction committed. Yield briefly, then retry the read.
                    self._event_condition.wait(min(0.05, remaining))
                observed_generation = self._event_generation

    def latest_event_sequence(self, db: sqlite3.Connection | None = None) -> int:
        owns = db is None
        db = db or self.connect()
        try:
            return int(db.execute("SELECT COALESCE(MAX(sequence),0) FROM job_events").fetchone()[0])
        finally:
            if owns:
                db.close()

    def event_bounds(self) -> tuple[int, int]:
        with self.connect() as db:
            row = db.execute("SELECT COALESCE(MIN(sequence),0),COALESCE(MAX(sequence),0) FROM job_events").fetchone()
            return int(row[0]), int(row[1])

    def interrupt_orphaned_jobs(self) -> None:
        if not self.path.exists():
            return
        with self._lock, self.connect() as db:
            rows = list(db.execute("SELECT id,kind FROM jobs WHERE status IN ('QUEUED','RUNNING','CANCEL_REQUESTED')"))
            for row in rows:
                status = "FAILED_RECOVERABLE" if row["kind"] == "RENDER" else "INTERRUPTED"
                message = "Application restarted before or while the job was active"
                db.execute(
                    "UPDATE jobs SET status=?,message=?,updated_at=? WHERE id=?",
                    (status, message, now_iso(), row["id"]),
                )
                if row["kind"] == "SCAN":
                    db.execute(
                        "UPDATE scan_generations SET status='INTERRUPTED',message=?,updated_at=? WHERE id=? "
                        "AND status IN ('QUEUED','RUNNING','CANCEL_REQUESTED')",
                        (message, now_iso(), row["id"]),
                    )
                elif row["kind"] == "RENDER":
                    db.execute(
                        "UPDATE artifacts SET status='FAILED_RECOVERABLE',details_json=?,updated_at=? "
                        "WHERE job_id=? AND status IN ('QUEUED','RENDERING')",
                        (json.dumps({"error": "APPLICATION_RESTARTED"}), now_iso(), row["id"]),
                    )
                self._event_db(db, row["id"], "RECOVERY", status, None, message, {})

    def upsert_job(self, job: dict[str, Any]) -> None:
        """Compatibility seam for legacy render tests."""
        with self._lock, self.connect() as db:
            if not db.execute("SELECT id FROM jobs WHERE id=?", (job["id"],)).fetchone():
                self._create_job_db(
                    db, job["id"], "RENDER", project_id=job.get("projectId"), message=job.get("message", "Queued")
                )
            self._transition_job_db(
                db,
                job["id"],
                job.get("status", "queued"),
                float(job.get("progress", 0)),
                job.get("message"),
                {"outputPath": job.get("outputPath")},
            )

    # Render plans, reviews, and artifacts

    def save_render_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO render_plans(id,project_id,project_revision,plan_digest,source_set_digest,"
                "provenance_revision,status,plan_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    plan["id"],
                    plan["projectId"],
                    plan["projectRevision"],
                    plan["planDigest"],
                    plan["sourceSetDigest"],
                    plan["provenanceRevision"],
                    plan["status"],
                    json.dumps(plan),
                    now_iso(),
                ),
            )
            row = db.execute(
                "SELECT plan_json FROM render_plans WHERE plan_digest=?", (plan["planDigest"],)
            ).fetchone()
            if not row:
                raise DomainError("INTERNAL_ERROR", "Render plan could not be persisted")
            return json.loads(row[0])

    def render_plan(self, plan_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT plan_json FROM render_plans WHERE id=?", (plan_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Render plan not found")
            return json.loads(row[0])

    def attest_review(self, plan_id: str, warnings: list[str]) -> dict[str, Any]:
        plan = self.render_plan(plan_id)
        project = self.project(plan["projectId"])
        if project["revision"] != plan["projectRevision"]:
            raise DomainError("PLAN_STALE", "Project changed after render plan was created")
        if int(project.get("provenanceRevision", 0)) != int(plan["provenanceRevision"]):
            raise DomainError("PLAN_STALE", "Provenance resolution changed after render plan was created")
        if set(plan.get("warningCodes", [])) - set(warnings):
            raise DomainError("VALIDATION_FAILED", "Every render-plan warning must be acknowledged")
        attestation = {
            "id": opaque_id("review"),
            "renderPlanId": plan_id,
            "projectId": plan["projectId"],
            "projectRevision": plan["projectRevision"],
            "planDigest": plan["planDigest"],
            "sourceSetDigest": plan["sourceSetDigest"],
            "provenanceRevision": plan["provenanceRevision"],
            "acknowledgedWarnings": sorted(set(warnings)),
            "createdAt": now_iso(),
        }
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO review_attestations(id,render_plan_id,project_id,project_revision,plan_digest,source_set_digest,"
                "provenance_revision,warnings_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    attestation["id"],
                    plan_id,
                    plan["projectId"],
                    plan["projectRevision"],
                    plan["planDigest"],
                    plan["sourceSetDigest"],
                    plan["provenanceRevision"],
                    json.dumps(attestation["acknowledgedWarnings"]),
                    attestation["createdAt"],
                ),
            )
            project["review"] = attestation
            db.execute(
                "UPDATE projects SET document_json=?,updated_at=? WHERE id=?",
                (json.dumps(project), now_iso(), project["id"]),
            )
        return attestation

    def review_for_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM review_attestations WHERE render_plan_id=? ORDER BY created_at DESC LIMIT 1", (plan_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "renderPlanId": row["render_plan_id"],
                "projectId": row["project_id"],
                "projectRevision": row["project_revision"],
                "planDigest": row["plan_digest"],
                "sourceSetDigest": row["source_set_digest"],
                "provenanceRevision": row["provenance_revision"],
                "acknowledgedWarnings": json.loads(row["warnings_json"]),
                "createdAt": row["created_at"],
            }

    def create_artifact(self, plan_id: str, output_grant_id: str, filename: str) -> dict[str, Any]:
        self.grant(output_grant_id, "WRITE_OUTPUT")
        artifact_id = opaque_id("artifact")
        timestamp = now_iso()
        manifest_filename = filename + ".manifest.json"
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO artifacts(id,render_plan_id,output_grant_id,filename,manifest_filename,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'PLANNED',?,?)",
                (artifact_id, plan_id, output_grant_id, filename, manifest_filename, timestamp, timestamp),
            )
        return self.artifact(artifact_id)

    def update_artifact(self, artifact_id: str, **updates: Any) -> dict[str, Any]:
        allowed = {"job_id", "status", "video_digest", "manifest_digest", "details_json"}
        values = {key: value for key, value in updates.items() if key in allowed}
        if not values:
            return self.artifact(artifact_id)
        assignments = ",".join(f"{key}=?" for key in values) + ",updated_at=?"
        params = [
            json.dumps(value) if key == "details_json" and not isinstance(value, str) else value
            for key, value in values.items()
        ]
        params += [now_iso(), artifact_id]
        with self._lock, self.connect() as db:
            db.execute(f"UPDATE artifacts SET {assignments} WHERE id=?", params)
        return self.artifact(artifact_id)

    def complete_render_artifact(
        self,
        job_id: str,
        artifact_id: str,
        video_digest: str,
        manifest_digest: str,
        details: dict[str, Any],
    ) -> bool:
        """Atomically publish durable success unless cancellation already owns the job."""
        with self._lock, self.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise DomainError("NOT_FOUND", "Job not found")
            artifact = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if not artifact:
                raise DomainError("NOT_FOUND", "Artifact not found")
            if artifact["job_id"] != job_id:
                raise DomainError("JOB_STATE_CONFLICT", "Artifact does not belong to render job")
            if job["status"] == "CANCEL_REQUESTED":
                return False
            if job["status"] != "RUNNING":
                raise DomainError("JOB_STATE_CONFLICT", "Only a running render may complete an artifact")
            db.execute(
                "UPDATE artifacts SET status='COMPLETE',video_digest=?,manifest_digest=?,details_json=?,updated_at=? "
                "WHERE id=?",
                (video_digest, manifest_digest, json.dumps(details), now_iso(), artifact_id),
            )
            self._transition_job_db(
                db,
                job_id,
                "SUCCEEDED",
                1,
                "Video and provenance manifest complete",
                result={"artifactId": artifact_id},
            )
            return True

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Artifact not found")
            return {
                "id": row["id"],
                "renderPlanId": row["render_plan_id"],
                "jobId": row["job_id"],
                "outputGrantId": row["output_grant_id"],
                "filename": row["filename"],
                "manifestFilename": row["manifest_filename"],
                "status": row["status"],
                "videoDigest": row["video_digest"],
                "manifestDigest": row["manifest_digest"],
                "details": json.loads(row["details_json"] or "{}"),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }

    def artifacts(self, *, incomplete_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            where = "WHERE status NOT IN ('COMPLETE','CANCELED','FAILED')" if incomplete_only else ""
            ids = [row[0] for row in db.execute(f"SELECT id FROM artifacts {where} ORDER BY created_at")]
        return [self.artifact(artifact_id) for artifact_id in ids]

    def output_path(self, grant_id: str, filename: str) -> Path:
        if not filename or Path(filename).name != filename or filename in {".", ".."}:
            raise DomainError("VALIDATION_FAILED", "Output filename must be a safe relative filename")
        grant = self.grant(grant_id, "WRITE_OUTPUT")
        root = Path(grant["root"]).resolve(strict=True)
        target = (root / filename).resolve()
        if not target.is_relative_to(root):
            raise DomainError("FORBIDDEN", "Output path escapes directory grant")
        return target


def _affected_program_intervals(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    boundaries = {0, int(before.get("durationUs", 0)), int(after.get("durationUs", 0))}
    for compiled in (before, after):
        for key in ("videoSlices", "audioSlices", "issues"):
            for item in compiled.get(key, []):
                boundaries.add(int(item.get("startUs", 0)))
                boundaries.add(int(item.get("endUs", compiled.get("durationUs", 0))))
    ordered = sorted(boundary for boundary in boundaries if boundary >= 0)
    result: list[dict[str, Any]] = []
    for start, end in zip(ordered, ordered[1:]):
        if start >= end:
            continue
        midpoint = start + (end - start) // 2
        old = _compiled_signature_at(before, midpoint)
        new = _compiled_signature_at(after, midpoint)
        if old == new:
            continue
        reasons = sorted(key for key in old if old[key] != new[key])
        if result and result[-1]["endUs"] == start and result[-1]["reasons"] == reasons:
            result[-1]["endUs"] = end
        else:
            result.append({"startUs": start, "endUs": end, "reasons": reasons})
    return result


def _compiled_signature_at(compiled: dict[str, Any], output_us: int) -> dict[str, Any]:
    def selected(name: str) -> dict[str, Any] | None:
        value = next(
            (
                item
                for item in compiled.get(name, [])
                if int(item.get("startUs", 0)) <= output_us < int(item.get("endUs", 0))
            ),
            None,
        )
        if value is None:
            return None
        return {
            key: value.get(key)
            for key in ("assetId", "streamId", "logicalSourceId", "sourceStartUs", "sourceEndUs", "synthetic")
        }

    return {
        "video": selected("videoSlices"),
        "audio": selected("audioSlices"),
        "issues": sorted(
            item.get("code")
            for item in compiled.get("issues", [])
            if int(item.get("startUs", 0)) <= output_us
            < int(item.get("endUs", compiled.get("durationUs", 0) + 1))
        ),
    }


def _normalize_media_record_timestamp(record: MediaRecord, policy: dict[str, Any]) -> None:
    if not isinstance(record.custom, dict):
        record.custom = {}
    previous_policy = record.custom.get("timestampPolicy", {})
    raw = previous_policy.get("rawValue") or record.captured_at or _raw_timestamp_from_evidence(record)
    if raw is None:
        return
    outcome = normalize_timestamp(
        raw,
        str(policy["time_zone"]),
        int(policy["dst_fold"]),
        str(policy["nonexistent_policy"]),
    )
    record.custom["timestampPolicy"] = outcome
    record.captured_at = outcome.get("resolvedUtc")
    evidence_value = {
        "rawValue": outcome["rawValue"],
        "resolvedUtc": outcome.get("resolvedUtc"),
        "timeZone": outcome["timeZone"],
        "ambiguity": outcome["ambiguity"],
        "dstFold": outcome["dstFold"],
        "nonexistentPolicy": outcome["nonexistentPolicy"],
    }
    if not any(
        item.kind == "importer"
        and item.field == "captured_at.normalization"
        and item.value == evidence_value
        for item in record.evidence
    ):
        record.evidence.append(
            ProvenanceEvidence(
                "importer",
                "captured_at.normalization",
                evidence_value,
                0.9 if outcome.get("resolvedUtc") else 0.2,
                "library-time-policy",
                raw_value=outcome["rawValue"],
                normalized_value=outcome.get("resolvedUtc"),
                uncertainty=outcome["ambiguity"],
            )
        )


def _raw_timestamp_from_evidence(record: MediaRecord) -> object | None:
    for item in reversed(record.evidence):
        if item.field == "captured_at" and item.origin != "library-time-policy":
            return item.raw_value if item.raw_value is not None else item.value
    return None


def _stable_migration_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{digest_json([str(part) for part in parts])[:24]}"


def _identity_key_from_fingerprint(fingerprint: dict[str, Any]) -> str | None:
    material = {
        "device": fingerprint.get("device"),
        "inode": fingerprint.get("inode"),
        "size": fingerprint.get("size"),
        "sampleSha256": fingerprint.get("sampleSha256"),
    }
    if any(material.get(key) is None for key in material):
        return None
    return digest_json(material)


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
