from __future__ import annotations

import base64
import binascii
import copy
import hashlib
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
    ClipAlignmentTransform,
    DomainError,
    alignment_digest,
    alignment_summary,
    apply_command,
    compile_program,
    digest_json,
    new_project,
    now_iso,
    opaque_id,
    project_preparation,
    seconds_to_us,
    timeline_window,
    timeline_section_proposal,
)
from .models import MediaRecord, ScanSummary
from .models import ProvenanceEvidence
from .provenance import normalize_timestamp
from .scanner import media_record_from_dict, quick_fingerprint


SCHEMA_VERSION = 9
PROJECT_SNAPSHOT_INTERVAL = 25
MAX_JOB_EVENTS = 100_000
MAX_CACHE_ENTRIES = 10_000
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_APPLICATION_SETTINGS = {
    "overlapSearchExtensionUs": 30_000_000,
    "textScalePercent": 100,
    "colorScheme": "DARKROOM",
}
COLOR_SCHEMES = {"DARKROOM", "SLATE", "DAYLIGHT", "HIGH_CONTRAST"}
TEXT_SCALES = {90, 100, 115, 130}
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
    "SetSyncTransform", "SetClipAlignment", "SetTimelineSections",
    "GenerateProgramDraft", "AddVideoBlock", "SplitVideoBlock", "MoveVideoBoundary",
    "DeleteVideoBlock", "AssignVideoSource", "PinVideoClip", "CutToSource", "AddAudioBlock",
    "SplitAudioBlock", "MoveAudioBoundary", "DeleteAudioBlock", "SetAudioMode", "SetAnchoringMode",
    "ReconcileBoundary", "AcceptAlignmentSuggestion", "AcceptAlignmentSuggestions",
    "AlignMarkedMoments", "AcceptAlignmentProposalSet", "AcceptAlignmentProposal",
}


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS application_settings (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  overlap_search_extension_us INTEGER NOT NULL DEFAULT 30000000,
  text_scale_percent INTEGER NOT NULL DEFAULT 100,
  color_scheme TEXT NOT NULL DEFAULT 'DARKROOM',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS directory_grants (
  id TEXT PRIMARY KEY,
  role TEXT NOT NULL CHECK(role IN ('READ_ONLY_SOURCE','WRITE_OUTPUT')),
  root TEXT NOT NULL,
  device INTEGER,
  inode INTEGER,
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
  name TEXT NOT NULL DEFAULT 'Video library',
  time_zone TEXT NOT NULL DEFAULT 'UTC',
  dst_fold INTEGER NOT NULL DEFAULT 0,
  nonexistent_policy TEXT NOT NULL DEFAULT 'REJECT',
  current_generation INTEGER NOT NULL DEFAULT 0,
  catalog_revision INTEGER NOT NULL DEFAULT 0,
  event_gap_us INTEGER NOT NULL DEFAULT 15000000,
  session_gap_us INTEGER NOT NULL DEFAULT 120000000,
  last_scan TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS library_roots (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  grant_id TEXT NOT NULL REFERENCES directory_grants(id),
  root TEXT NOT NULL,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  time_policy_json TEXT,
  last_scan_at TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  UNIQUE(library_id, root)
);
CREATE INDEX IF NOT EXISTS library_roots_library_active ON library_roots(library_id,active,id);
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
  root_ids_json TEXT NOT NULL DEFAULT '[]',
  root_progress_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(library_id, generation)
);
CREATE TABLE IF NOT EXISTS scan_roots (
  scan_id TEXT NOT NULL REFERENCES scan_generations(id) ON DELETE CASCADE,
  root_id TEXT NOT NULL REFERENCES library_roots(id),
  status TEXT NOT NULL,
  scanned INTEGER NOT NULL DEFAULT 0,
  warnings INTEGER NOT NULL DEFAULT 0,
  full_traversal_completed INTEGER NOT NULL DEFAULT 0,
  message TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scan_id, root_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_scan_per_library
  ON scan_generations(library_id) WHERE status IN ('QUEUED','RUNNING','CANCEL_REQUESTED');
CREATE TABLE IF NOT EXISTS media (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  root_id TEXT REFERENCES library_roots(id),
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
CREATE INDEX IF NOT EXISTS media_root_path ON media(root_id, relative_path, id);
CREATE INDEX IF NOT EXISTS media_library_time ON media(library_id, captured_at);
CREATE INDEX IF NOT EXISTS media_library_time_id ON media(library_id, captured_at, id);
CREATE INDEX IF NOT EXISTS media_library_camera ON media(library_id, camera);
CREATE TABLE IF NOT EXISTS catalog_revisions (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  scan_id TEXT REFERENCES scan_generations(id),
  digest TEXT NOT NULL,
  asset_count INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(library_id, revision)
);
CREATE TABLE IF NOT EXISTS cluster_generations (
  id TEXT PRIMARY KEY,
  library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  catalog_revision_id TEXT NOT NULL REFERENCES catalog_revisions(id),
  catalog_revision INTEGER NOT NULL,
  job_id TEXT NOT NULL UNIQUE,
  algorithm TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  config_json TEXT NOT NULL,
  config_digest TEXT NOT NULL,
  status TEXT NOT NULL,
  session_count INTEGER NOT NULL DEFAULT 0,
  event_count INTEGER NOT NULL DEFAULT 0,
  clustered_asset_count INTEGER NOT NULL DEFAULT 0,
  unclustered_asset_count INTEGER NOT NULL DEFAULT 0,
  message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cluster_generations_library_created
  ON cluster_generations(library_id,created_at DESC,id);
CREATE TABLE IF NOT EXISTS session_clusters (
  id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL REFERENCES cluster_generations(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  start_us INTEGER NOT NULL,
  end_us INTEGER NOT NULL,
  event_count INTEGER NOT NULL DEFAULT 0,
  clip_count INTEGER NOT NULL DEFAULT 0,
  source_count INTEGER NOT NULL DEFAULT 0,
  root_count INTEGER NOT NULL DEFAULT 0,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(generation_id, ordinal)
);
CREATE INDEX IF NOT EXISTS session_clusters_generation_time
  ON session_clusters(generation_id,start_us,id);
CREATE TABLE IF NOT EXISTS event_clusters (
  id TEXT PRIMARY KEY,
  generation_id TEXT NOT NULL REFERENCES cluster_generations(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES session_clusters(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  session_ordinal INTEGER NOT NULL,
  start_us INTEGER NOT NULL,
  end_us INTEGER NOT NULL,
  clip_count INTEGER NOT NULL DEFAULT 0,
  source_count INTEGER NOT NULL DEFAULT 0,
  root_count INTEGER NOT NULL DEFAULT 0,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  UNIQUE(generation_id, ordinal)
);
CREATE INDEX IF NOT EXISTS event_clusters_generation_time
  ON event_clusters(generation_id,start_us,id);
CREATE INDEX IF NOT EXISTS event_clusters_session_time
  ON event_clusters(session_id,start_us,id);
CREATE TABLE IF NOT EXISTS cluster_memberships (
  generation_id TEXT NOT NULL REFERENCES cluster_generations(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL REFERENCES session_clusters(id) ON DELETE CASCADE,
  event_id TEXT NOT NULL REFERENCES event_clusters(id) ON DELETE CASCADE,
  asset_id TEXT NOT NULL REFERENCES media(id),
  start_us INTEGER NOT NULL,
  end_us INTEGER NOT NULL,
  root_id TEXT,
  source_candidate_id TEXT,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(generation_id, asset_id)
);
CREATE INDEX IF NOT EXISTS cluster_memberships_event_time
  ON cluster_memberships(event_id,start_us,asset_id);
CREATE INDEX IF NOT EXISTS cluster_memberships_session_time
  ON cluster_memberships(session_id,start_us,asset_id);
CREATE INDEX IF NOT EXISTS cluster_memberships_generation_root
  ON cluster_memberships(generation_id,root_id,asset_id);
CREATE INDEX IF NOT EXISTS cluster_memberships_generation_source
  ON cluster_memberships(generation_id,source_candidate_id,asset_id);
CREATE TABLE IF NOT EXISTS unclustered_memberships (
  generation_id TEXT NOT NULL REFERENCES cluster_generations(id) ON DELETE CASCADE,
  asset_id TEXT NOT NULL REFERENCES media(id),
  warnings_json TEXT NOT NULL DEFAULT '["TIMESTAMP_UNRESOLVED"]',
  PRIMARY KEY(generation_id, asset_id)
);
CREATE INDEX IF NOT EXISTS unclustered_memberships_generation_asset
  ON unclustered_memberships(generation_id,asset_id);
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
CREATE TABLE IF NOT EXISTS project_revision_deltas (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  base_revision INTEGER NOT NULL,
  delta_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(project_id, revision)
);
CREATE TABLE IF NOT EXISTS project_components (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  component_type TEXT NOT NULL,
  component_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  document_json TEXT NOT NULL,
  updated_revision INTEGER NOT NULL,
  PRIMARY KEY(project_id, component_type, component_id)
);
CREATE INDEX IF NOT EXISTS project_components_order
  ON project_components(project_id,component_type,ordinal,component_id);
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
CREATE TABLE IF NOT EXISTS alignment_proposal_sets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  project_revision INTEGER NOT NULL,
  selection_digest TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  proposal_digest TEXT NOT NULL UNIQUE,
  algorithm TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  config_digest TEXT NOT NULL,
  status TEXT NOT NULL,
  set_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alignment_sets_project_created
  ON alignment_proposal_sets(project_id,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS alignment_acceptance_previews (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  project_revision INTEGER NOT NULL,
  proposal_set_id TEXT NOT NULL REFERENCES alignment_proposal_sets(id) ON DELETE CASCADE,
  proposal_digest TEXT NOT NULL,
  preview_digest TEXT NOT NULL UNIQUE,
  expires_at REAL NOT NULL,
  preview_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alignment_previews_project_created
  ON alignment_acceptance_previews(project_id,created_at DESC,id DESC);
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
            db.execute(
                "INSERT OR IGNORE INTO application_settings(singleton,overlap_search_extension_us,"
                "text_scale_percent,color_scheme,updated_at) VALUES(1,?,?,?,?)",
                (
                    DEFAULT_APPLICATION_SETTINGS["overlapSearchExtensionUs"],
                    DEFAULT_APPLICATION_SETTINGS["textScalePercent"],
                    DEFAULT_APPLICATION_SETTINGS["colorScheme"],
                    now_iso(),
                ),
            )
            self._ensure_legacy_columns(db)
            self._backfill_directory_grant_identities(db)
            self._backfill_library_roots(db)
            self._backfill_catalog_revisions(db)
            db.execute(
                "INSERT OR IGNORE INTO project_revisions(project_id,revision,document_json,created_at) "
                "SELECT id,revision,document_json,updated_at FROM projects"
            )
            self._backfill_project_components(db)
            self._stale_legacy_alignment_proposals(db)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at,details_json) VALUES(?,?,?)",
                (SCHEMA_VERSION, now_iso(), json.dumps({"name": "canonical-v1"})),
            )
        self.interrupt_orphaned_jobs()
        self.compact_events()
        self.prune_cache()

    @staticmethod
    def _stale_legacy_alignment_proposals(db: sqlite3.Connection) -> None:
        rows = list(
            db.execute(
                "SELECT id,set_json FROM alignment_proposal_sets "
                "WHERE algorithm='bounded-audio-evidence-graph' AND algorithm_version!='3' "
                "AND status IN ('PENDING','PARTIALLY_RESOLVED')"
            )
        )
        for row in rows:
            value = json.loads(row["set_json"])
            value["status"] = "STALE"
            value["invalidationReason"] = "Alignment solver version changed"
            value["updatedAt"] = now_iso()
            db.execute(
                "UPDATE alignment_proposal_sets SET status='STALE',set_json=?,updated_at=? WHERE id=?",
                (json.dumps(value), value["updatedAt"], row["id"]),
            )

    def application_settings(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT overlap_search_extension_us,text_scale_percent,color_scheme,updated_at "
                "FROM application_settings WHERE singleton=1"
            ).fetchone()
            if not row:
                return {**DEFAULT_APPLICATION_SETTINGS, "updatedAt": None}
            return {
                "overlapSearchExtensionUs": int(row["overlap_search_extension_us"]),
                "textScalePercent": int(row["text_scale_percent"]),
                "colorScheme": str(row["color_scheme"]),
                "updatedAt": row["updated_at"],
            }

    def update_application_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        allowed = {"overlapSearchExtensionUs", "textScalePercent", "colorScheme"}
        unknown = set(settings) - allowed
        if unknown:
            raise DomainError(
                "VALIDATION_FAILED",
                f"Unknown application setting: {sorted(unknown)[0]}",
            )
        current = self.application_settings()
        def integer_setting(name: str, fallback: object) -> int:
            value = settings.get(name, fallback)
            if not isinstance(value, int) or isinstance(value, bool):
                raise DomainError("VALIDATION_FAILED", f"{name} must be an integer")
            return value

        overlap_us = integer_setting(
            "overlapSearchExtensionUs", current["overlapSearchExtensionUs"]
        )
        text_scale = integer_setting("textScalePercent", current["textScalePercent"])
        color_scheme = str(settings.get("colorScheme", current["colorScheme"]))
        if not 0 <= overlap_us <= 300_000_000:
            raise DomainError(
                "VALIDATION_FAILED",
                "Overlap search extension must be between 0 and 300 seconds",
            )
        if text_scale not in TEXT_SCALES:
            raise DomainError("VALIDATION_FAILED", "Text scale must be 90, 100, 115, or 130 percent")
        if color_scheme not in COLOR_SCHEMES:
            raise DomainError("VALIDATION_FAILED", "Unknown color scheme")
        updated_at = now_iso()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO application_settings(singleton,overlap_search_extension_us,"
                "text_scale_percent,color_scheme,updated_at) VALUES(1,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET overlap_search_extension_us=excluded.overlap_search_extension_us,"
                "text_scale_percent=excluded.text_scale_percent,color_scheme=excluded.color_scheme,"
                "updated_at=excluded.updated_at",
                (overlap_us, text_scale, color_scheme, updated_at),
            )
            if overlap_us != int(current["overlapSearchExtensionUs"]):
                rows = list(
                    db.execute(
                        "SELECT id,set_json FROM alignment_proposal_sets "
                        "WHERE status IN ('PENDING','PARTIALLY_RESOLVED')"
                    )
                )
                for row in rows:
                    value = json.loads(row["set_json"])
                    value["status"] = "STALE"
                    value["invalidationReason"] = "Overlap search settings changed"
                    value["updatedAt"] = updated_at
                    db.execute(
                        "UPDATE alignment_proposal_sets SET status='STALE',set_json=?,updated_at=? WHERE id=?",
                        (json.dumps(value), updated_at, row["id"]),
                    )
        return {
            "overlapSearchExtensionUs": overlap_us,
            "textScalePercent": text_scale,
            "colorScheme": color_scheme,
            "updatedAt": updated_at,
        }

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
        destination.row_factory = sqlite3.Row
        try:
            source.backup(destination)
            # Existing tables keep their old shape when CREATE TABLE IF NOT
            # EXISTS runs. Add columns referenced by new indexes before the
            # full schema creates those indexes.
            self._ensure_legacy_columns(destination)
            destination.executescript(SCHEMA)
            self._ensure_legacy_columns(destination)
            self._backfill_directory_grant_identities(destination)
            self._backfill_library_roots(destination)
            self._backfill_catalog_revisions(destination)
            self._backfill_project_components(destination)
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
            "directory_grants": {
                "device": "INTEGER",
                "inode": "INTEGER",
            },
            "libraries": {
                "grant_id": "TEXT REFERENCES directory_grants(id)",
                "name": "TEXT NOT NULL DEFAULT 'Video library'",
                "time_zone": "TEXT NOT NULL DEFAULT 'UTC'",
                "dst_fold": "INTEGER NOT NULL DEFAULT 0",
                "nonexistent_policy": "TEXT NOT NULL DEFAULT 'REJECT'",
                "current_generation": "INTEGER NOT NULL DEFAULT 0",
                "catalog_revision": "INTEGER NOT NULL DEFAULT 0",
                "event_gap_us": "INTEGER NOT NULL DEFAULT 15000000",
                "session_gap_us": "INTEGER NOT NULL DEFAULT 120000000",
            },
            "scan_generations": {
                "root_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "root_progress_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "media": {
                "root_id": "TEXT REFERENCES library_roots(id)",
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
            if not existing:
                continue
            for name, definition in columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _backfill_directory_grant_identities(self, db: sqlite3.Connection) -> None:
        for row in db.execute(
            "SELECT id,root FROM directory_grants WHERE device IS NULL OR inode IS NULL"
        ):
            try:
                resolved = Path(row["root"]).resolve(strict=True)
                stat = resolved.stat()
            except OSError:
                continue
            if str(resolved) != row["root"]:
                continue
            db.execute(
                "UPDATE directory_grants SET device=?,inode=? WHERE id=?",
                (stat.st_dev, stat.st_ino, row["id"]),
            )

    def _backfill_library_roots(self, db: sqlite3.Connection) -> None:
        for library in db.execute(
            "SELECT id,grant_id,root,name FROM libraries WHERE grant_id IS NOT NULL ORDER BY id"
        ):
            root_id = f"root_{digest_json({'libraryId': library['id'], 'root': library['root']})[:24]}"
            label = Path(library["root"]).name or str(library["name"] or "Video folder")
            db.execute(
                "INSERT OR IGNORE INTO library_roots(id,library_id,grant_id,root,label,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (root_id, library["id"], library["grant_id"], library["root"], label, now_iso()),
            )
            rows = list(
                db.execute(
                    "SELECT id,relative_path,record_json FROM media WHERE library_id=? AND root_id IS NULL",
                    (library["id"],),
                )
            )
            for row in rows:
                payload = json.loads(row["record_json"])
                relative = str(payload.get("relative_path") or row["relative_path"])
                payload["rootId"] = root_id
                db.execute(
                    "UPDATE media SET root_id=?,relative_path=?,record_json=? WHERE id=?",
                    (root_id, _storage_relative_path(root_id, relative), json.dumps(payload), row["id"]),
                )

    def _backfill_catalog_revisions(self, db: sqlite3.Connection) -> None:
        for library in db.execute(
            "SELECT id,current_generation,catalog_revision FROM libraries ORDER BY id"
        ):
            revision = int(library["catalog_revision"])
            if revision == 0 and int(library["current_generation"]) > 0:
                revision = 1
                db.execute(
                    "UPDATE libraries SET catalog_revision=1 WHERE id=?", (library["id"],)
                )
            if revision <= 0:
                continue
            exists = db.execute(
                "SELECT 1 FROM catalog_revisions WHERE library_id=? AND revision=?",
                (library["id"], revision),
            ).fetchone()
            if exists:
                continue
            digest, asset_count = self._catalog_digest_db(db, library["id"])
            revision_id = _stable_migration_id(
                "catalog", library["id"], revision, digest
            )
            db.execute(
                "INSERT INTO catalog_revisions(id,library_id,revision,digest,asset_count,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (revision_id, library["id"], revision, digest, asset_count, now_iso()),
            )

    def _backfill_project_components(self, db: sqlite3.Connection) -> None:
        for row in db.execute("SELECT id,revision,document_json FROM projects"):
            exists = db.execute(
                "SELECT 1 FROM project_components WHERE project_id=? LIMIT 1", (row["id"],)
            ).fetchone()
            if exists:
                continue
            project = self._migrate_legacy_project(json.loads(row["document_json"]))
            self._sync_project_component_delta_db(db, {}, project, int(row["revision"]))

    @staticmethod
    def _sync_project_component_delta_db(
        db: sqlite3.Connection,
        before: dict[str, Any],
        after: dict[str, Any],
        revision: int,
    ) -> None:
        component_fields = {
            "logicalSource": "logicalSources",
            "clip": "clips",
            "timelineSection": "timelineSections",
            "videoBlock": "videoBlocks",
            "audioBlock": "audioBlocks",
            "syntheticSlate": "syntheticSlates",
        }
        project_id = str(after["id"])
        for component_type, field in component_fields.items():
            previous = {str(item["id"]): item for item in before.get(field, [])}
            current = {str(item["id"]): item for item in after.get(field, [])}
            removed = sorted(set(previous) - set(current))
            if removed:
                db.executemany(
                    "DELETE FROM project_components WHERE project_id=? AND component_type=? AND component_id=?",
                    ((project_id, component_type, component_id) for component_id in removed),
                )
            rows = []
            for ordinal, item in enumerate(after.get(field, [])):
                component_id = str(item["id"])
                if previous.get(component_id) == item:
                    continue
                rows.append(
                    (
                        project_id,
                        component_type,
                        component_id,
                        ordinal,
                        json.dumps(item),
                        int(revision),
                    )
                )
            if rows:
                db.executemany(
                    "INSERT INTO project_components(project_id,component_type,component_id,ordinal,document_json,updated_revision) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,component_type,component_id) DO UPDATE SET "
                    "ordinal=excluded.ordinal,document_json=excluded.document_json,updated_revision=excluded.updated_revision",
                    rows,
                )

    @staticmethod
    def _catalog_digest_db(db: sqlite3.Connection, library_id: str) -> tuple[str, int]:
        digest = hashlib.sha256()
        count = 0
        cursor = db.execute(
            "SELECT id,root_id,relative_path,captured_at,duration,missing,fingerprint_json "
            "FROM media WHERE library_id=? ORDER BY id",
            (library_id,),
        )
        while rows := cursor.fetchmany(1000):
            for row in rows:
                digest.update(
                    json.dumps(
                        [
                            row["id"],
                            row["root_id"],
                            row["relative_path"],
                            row["captured_at"],
                            row["duration"],
                            int(row["missing"]),
                            json.loads(row["fingerprint_json"] or "{}"),
                        ],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                digest.update(b"\n")
                count += 1
        return digest.hexdigest(), count

    def _advance_catalog_revision_db(
        self, db: sqlite3.Connection, library_id: str, scan_id: str | None = None
    ) -> int:
        db.execute(
            "UPDATE libraries SET catalog_revision=catalog_revision+1 WHERE id=?", (library_id,)
        )
        revision = int(
            db.execute(
                "SELECT catalog_revision FROM libraries WHERE id=?", (library_id,)
            ).fetchone()[0]
        )
        catalog_digest, asset_count = self._catalog_digest_db(db, library_id)
        db.execute(
            "INSERT INTO catalog_revisions(id,library_id,revision,scan_id,digest,asset_count,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                opaque_id("catalog"),
                library_id,
                revision,
                scan_id,
                catalog_digest,
                asset_count,
                now_iso(),
            ),
        )
        self._invalidate_alignment_sets_for_library_db(
            db, library_id, "Library catalog changed after alignment analysis"
        )
        return revision

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
        identity = resolved.stat()
        with self._lock, self.connect() as db:
            for row in db.execute("SELECT root,role FROM directory_grants WHERE revoked=0"):
                other = Path(row["root"])
                if role != row["role"] and (_contains(other, resolved) or _contains(resolved, other)):
                    raise DomainError("FORBIDDEN", "Source and output grants may not overlap")
            existing = db.execute(
                "SELECT * FROM directory_grants WHERE root=? AND role=? AND revoked=0", (str(resolved), role)
            ).fetchone()
            if existing:
                self._validated_grant_root(existing)
                return self._public_grant(existing)
            grant_id = opaque_id("grant")
            db.execute(
                "INSERT INTO directory_grants(id,role,root,device,inode,created_at) VALUES(?,?,?,?,?,?)",
                (grant_id, role, str(resolved), identity.st_dev, identity.st_ino, now_iso()),
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
            self._validated_grant_root(row)
            return dict(row)

    @staticmethod
    def _validated_grant_root(row: sqlite3.Row) -> Path:
        if row["device"] is None or row["inode"] is None:
            raise DomainError("GRANT_REQUIRED", "Directory must be granted again")
        try:
            configured = Path(row["root"])
            resolved = configured.resolve(strict=True)
            stat = resolved.stat()
        except OSError as error:
            raise DomainError("GRANT_REQUIRED", "Directory grant is unavailable") from error
        if (
            str(resolved) != row["root"]
            or stat.st_dev != int(row["device"])
            or stat.st_ino != int(row["inode"])
        ):
            raise DomainError("GRANT_REQUIRED", "Directory grant identity changed")
        return resolved

    def revoke_grant(self, grant_id: str) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM directory_grants WHERE id=?", (grant_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Directory grant not found")
            db.execute("UPDATE directory_grants SET revoked=1,revoked_at=? WHERE id=?", (now_iso(), grant_id))
            revoked_at = now_iso()
            affected_roots = [
                item["id"]
                for item in db.execute(
                    "SELECT id FROM library_roots WHERE grant_id=? AND active=1", (grant_id,)
                )
            ]
            db.execute(
                "UPDATE library_roots SET active=0,revoked_at=? WHERE grant_id=?",
                (revoked_at, grant_id),
            )
            if affected_roots:
                db.execute(
                    f"UPDATE media SET missing=1 WHERE root_id IN ({','.join('?' for _ in affected_roots)})",
                    affected_roots,
                )
            dependent_jobs = list(
                db.execute(
                    "SELECT DISTINCT jobs.id FROM jobs LEFT JOIN projects ON projects.id=jobs.project_id "
                    "LEFT JOIN artifacts ON artifacts.job_id=jobs.id "
                    "WHERE jobs.status IN ('QUEUED','RUNNING','CANCEL_REQUESTED') AND ("
                    "jobs.library_id IN (SELECT id FROM libraries WHERE grant_id=? UNION SELECT library_id FROM library_roots WHERE grant_id=?) OR "
                    "projects.library_id IN (SELECT id FROM libraries WHERE grant_id=? UNION SELECT library_id FROM library_roots WHERE grant_id=?) OR "
                    "artifacts.output_grant_id=?)",
                    (grant_id, grant_id, grant_id, grant_id, grant_id),
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
        """Compatibility creator for a one-root library.

        The v1 HTTP contract creates an empty named library and then adds roots;
        legacy callers retain this atomic convenience without changing behavior.
        """
        grant = self.grant(grant_id, "READ_ONLY_SOURCE")
        with self.connect() as db:
            existing = db.execute(
                "SELECT library_id FROM library_roots WHERE grant_id=? AND active=1",
                (grant_id,),
            ).fetchone()
        if existing:
            self.update_library_time_policy(
                existing["library_id"], time_zone, dst_fold, nonexistent_policy
            )
            return self.library(existing["library_id"])
        library = self.create_empty_library(
            Path(grant["root"]).name or "Video library",
            time_zone,
            dst_fold,
            nonexistent_policy,
        )
        self.add_library_root(library["id"], grant_id)
        return self.library(library["id"])

    def create_empty_library(
        self,
        name: str,
        time_zone: str = "UTC",
        dst_fold: int = 0,
        nonexistent_policy: str = "REJECT",
        event_gap_us: int = 15_000_000,
        session_gap_us: int = 120_000_000,
    ) -> dict[str, Any]:
        self._validate_library_settings(
            time_zone, nonexistent_policy, event_gap_us, session_gap_us
        )
        label = str(name).strip()[:200]
        if not label:
            raise DomainError("VALIDATION_FAILED", "Library name is required")
        library_id = opaque_id("lib")
        placeholder = f".__library__/{library_id}"
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO libraries(id,grant_id,root,name,time_zone,dst_fold,nonexistent_policy,"
                "event_gap_us,session_gap_us,summary_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    library_id,
                    None,
                    placeholder,
                    label,
                    time_zone,
                    int(bool(dst_fold)),
                    nonexistent_policy,
                    int(event_gap_us),
                    int(session_gap_us),
                    "{}",
                ),
            )
        return self.library(library_id)

    @staticmethod
    def _validate_library_settings(
        time_zone: str, nonexistent_policy: str, event_gap_us: int, session_gap_us: int
    ) -> None:
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise DomainError("VALIDATION_FAILED", "Library timeZone must be a valid IANA zone") from error
        if nonexistent_policy not in {"REJECT", "SHIFT_FORWARD"}:
            raise DomainError("VALIDATION_FAILED", "Unknown nonexistent local-time policy")
        if int(event_gap_us) < 0 or int(session_gap_us) < int(event_gap_us):
            raise DomainError(
                "VALIDATION_FAILED", "sessionGapUs must be greater than or equal to eventGapUs"
            )

    def add_library_root(
        self,
        library_id: str,
        grant_id: str,
        label: str | None = None,
        time_policy_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        library = self.library(library_id)
        grant = self.grant(grant_id, "READ_ONLY_SOURCE")
        resolved = Path(grant["root"]).resolve(strict=True)
        normalized_override = None
        if time_policy_override is not None:
            unknown = set(time_policy_override) - {"timeZone", "dstFold", "nonexistentPolicy"}
            if unknown:
                raise DomainError("VALIDATION_FAILED", "Unknown root time-policy field")
            normalized_override = {
                "timeZone": str(time_policy_override.get("timeZone", library["timeZone"])),
                "dstFold": int(bool(time_policy_override.get("dstFold", library["dstFold"]))),
                "nonexistentPolicy": str(
                    time_policy_override.get("nonexistentPolicy", library["nonexistentPolicy"])
                ),
            }
            self._validate_library_settings(
                normalized_override["timeZone"],
                normalized_override["nonexistentPolicy"],
                0,
                0,
            )
        with self._lock, self.connect() as db:
            historical = db.execute(
                "SELECT * FROM library_roots WHERE library_id=? AND root=?",
                (library_id, str(resolved)),
            ).fetchone()
            roots = list(
                db.execute(
                    "SELECT * FROM library_roots WHERE library_id=? AND active=1 ORDER BY created_at,id",
                    (library_id,),
                )
            )
            if len(roots) >= 16:
                raise DomainError("VALIDATION_FAILED", "A library may contain at most 16 active roots")
            for existing in roots:
                other = Path(existing["root"])
                if _contains(other, resolved) or _contains(resolved, other):
                    raise DomainError(
                        "VALIDATION_FAILED",
                        "Library roots may not be duplicate, nested, or overlapping",
                    )
            root_id = opaque_id("root")
            timestamp = now_iso()
            root_label = str(
                label or (historical["label"] if historical else None) or resolved.name or "Video folder"
            ).strip()[:200]
            if historical:
                root_id = historical["id"]
                policy_json = (
                    json.dumps(normalized_override)
                    if normalized_override is not None
                    else historical["time_policy_json"]
                )
                db.execute(
                    "UPDATE library_roots SET grant_id=?,label=?,active=1,time_policy_json=?,"
                    "revoked_at=NULL WHERE id=?",
                    (grant_id, root_label, policy_json, root_id),
                )
            else:
                db.execute(
                    "INSERT INTO library_roots(id,library_id,grant_id,root,label,active,time_policy_json,created_at) "
                    "VALUES(?,?,?,?,?,1,?,?)",
                    (
                        root_id,
                        library_id,
                        grant_id,
                        str(resolved),
                        root_label,
                        json.dumps(normalized_override) if normalized_override is not None else None,
                        timestamp,
                    ),
                )
            if not roots:
                db.execute(
                    "UPDATE libraries SET grant_id=?,root=? WHERE id=?",
                    (grant_id, str(resolved), library_id),
                )
            row = db.execute("SELECT * FROM library_roots WHERE id=?", (root_id,)).fetchone()
            return self._public_library_root(row)

    def library_roots(self, library_id: str, *, include_revoked: bool = True) -> list[dict[str, Any]]:
        self.library(library_id)
        with self.connect() as db:
            where = "" if include_revoked else "AND active=1"
            return [
                self._public_library_root(row)
                for row in db.execute(
                    f"SELECT * FROM library_roots WHERE library_id=? {where} ORDER BY created_at,id",
                    (library_id,),
                )
            ]

    @staticmethod
    def _public_library_root(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "libraryId": row["library_id"],
            "grantId": row["grant_id"],
            "label": row["label"],
            "active": bool(row["active"]),
            "timePolicyOverride": json.loads(row["time_policy_json"]) if row["time_policy_json"] else None,
            "lastScanAt": row["last_scan_at"],
            "createdAt": row["created_at"],
            "revokedAt": row["revoked_at"],
        }

    def revoke_library_root(self, library_id: str, root_id: str) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            row = db.execute(
                "SELECT * FROM library_roots WHERE id=? AND library_id=?",
                (root_id, library_id),
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Library root not found")
            if not row["active"]:
                return self._public_library_root(row)
            timestamp = now_iso()
            db.execute(
                "UPDATE library_roots SET active=0,revoked_at=? WHERE id=?",
                (timestamp, root_id),
            )
            db.execute("UPDATE media SET missing=1 WHERE root_id=?", (root_id,))
            replacement = db.execute(
                "SELECT grant_id,root FROM library_roots WHERE library_id=? AND active=1 ORDER BY created_at,id LIMIT 1",
                (library_id,),
            ).fetchone()
            db.execute(
                "UPDATE libraries SET grant_id=?,root=? WHERE id=?",
                (
                    replacement["grant_id"] if replacement else None,
                    replacement["root"] if replacement else f".__library__/{library_id}",
                    library_id,
                ),
            )
            self._advance_catalog_revision_db(db, library_id)
        self.revoke_grant(row["grant_id"])
        with self.connect() as db:
            return self._public_library_root(
                db.execute("SELECT * FROM library_roots WHERE id=?", (root_id,)).fetchone()
            )

    def update_library_time_policy(
        self,
        library_id: str,
        time_zone: str,
        dst_fold: int = 0,
        nonexistent_policy: str = "REJECT",
    ) -> dict[str, Any]:
        self._validate_library_settings(time_zone, nonexistent_policy, 0, 0)
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Library not found")
            changed = (
                row["time_zone"] != time_zone
                or int(row["dst_fold"]) != int(bool(dst_fold))
                or row["nonexistent_policy"] != nonexistent_policy
            )
            db.execute(
                "UPDATE libraries SET time_zone=?,dst_fold=?,nonexistent_policy=? WHERE id=?",
                (time_zone, int(bool(dst_fold)), nonexistent_policy, library_id),
            )
            if changed:
                self._renormalize_library_timestamps_db(
                    db, library_id, time_zone, int(bool(dst_fold)), nonexistent_policy
                )
                self._invalidate_suggestions_db(
                    db,
                    "library_id=? AND status IN ('PENDING','ACCEPTED')",
                    (library_id,),
                    "Library timestamp policy changed",
                )
                self._advance_catalog_revision_db(db, library_id)
        return self.library(library_id)

    def library(self, library_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM libraries WHERE id=?", (library_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Library not found")
            return self._public_library(db, row)

    def libraries(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                self._public_library(db, row)
                for row in db.execute("SELECT * FROM libraries ORDER BY COALESCE(last_scan,'') DESC,id")
            ]

    @classmethod
    def _public_library(cls, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        roots = [
            cls._public_library_root(item)
            for item in db.execute(
                "SELECT * FROM library_roots WHERE library_id=? ORDER BY created_at,id",
                (row["id"],),
            )
        ]
        return {
            "id": row["id"],
            "sourceGrantId": row["grant_id"],
            "name": row["name"],
            "label": row["name"],
            "timeZone": row["time_zone"],
            "dstFold": int(row["dst_fold"]),
            "nonexistentPolicy": row["nonexistent_policy"],
            "currentGeneration": int(row["current_generation"]),
            "catalogRevision": int(row["catalog_revision"]),
            "eventGapUs": int(row["event_gap_us"]),
            "sessionGapUs": int(row["session_gap_us"]),
            "roots": roots,
            "lastScan": row["last_scan"],
            "summary": json.loads(row["summary_json"] or "{}"),
        }

    def library_root(self, library_id: str) -> Path:
        roots = self.active_library_root_paths(library_id)
        if not roots:
            raise DomainError("GRANT_REQUIRED", "Library has no active source folders")
        return roots[0][1]

    def active_library_root_paths(
        self, library_id: str, root_ids: Iterable[str] | None = None
    ) -> list[tuple[str, Path]]:
        selected = list(dict.fromkeys(root_ids or []))
        with self.connect() as db:
            library = db.execute(
                "SELECT id,grant_id FROM libraries WHERE id=?", (library_id,)
            ).fetchone()
            if not library:
                raise DomainError("NOT_FOUND", "Unknown library")
            if library["grant_id"] is None:
                revoked = db.execute(
                    "SELECT 1 FROM library_roots roots JOIN directory_grants grants "
                    "ON grants.id=roots.grant_id WHERE roots.library_id=? AND grants.revoked=1 LIMIT 1",
                    (library_id,),
                ).fetchone()
                if revoked:
                    raise DomainError("GRANT_REQUIRED", "Library source grant has been revoked")
                raise DomainError("GRANT_REQUIRED", "Library has no active source folders")
            query = (
                "SELECT roots.id,roots.root,roots.grant_id,grants.root AS grant_root,grants.device,"
                "grants.inode,grants.revoked "
                "FROM library_roots roots JOIN directory_grants grants ON grants.id=roots.grant_id "
                "WHERE roots.library_id=? AND roots.active=1"
            )
            params: list[Any] = [library_id]
            if selected:
                query += f" AND roots.id IN ({','.join('?' for _ in selected)})"
                params.extend(selected)
            query += " ORDER BY roots.created_at,roots.id"
            rows = list(db.execute(query, params))
            if not rows and not selected:
                revoked = db.execute(
                    "SELECT 1 FROM library_roots roots JOIN directory_grants grants "
                    "ON grants.id=roots.grant_id WHERE roots.library_id=? AND grants.revoked=1 LIMIT 1",
                    (library_id,),
                ).fetchone()
                if revoked:
                    raise DomainError("GRANT_REQUIRED", "Library source grant has been revoked")
        if selected and {row["id"] for row in rows} != set(selected):
            raise DomainError("GRANT_REQUIRED", "One or more selected library roots are unavailable")
        result: list[tuple[str, Path]] = []
        for row in rows:
            if row["revoked"] or row["root"] != row["grant_root"]:
                raise DomainError("GRANT_REQUIRED", "Library source grant has been revoked")
            result.append((row["id"], self._validated_grant_root(row)))
        return result

    # Scans and media

    def begin_scan(
        self,
        library_id: str,
        mode: str,
        limit: int | None = None,
        root_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if mode not in {"FULL", "INCREMENTAL", "BOUNDED"}:
            raise DomainError("VALIDATION_FAILED", "Unknown scan mode")
        roots = self.active_library_root_paths(library_id, root_ids)
        selected_root_ids = [item[0] for item in roots]
        with self._lock, self.connect() as db:
            active = db.execute(
                "SELECT id FROM scan_generations WHERE library_id=? AND status IN ('QUEUED','RUNNING','CANCEL_REQUESTED')",
                (library_id,),
            ).fetchone()
            if active:
                raise DomainError("JOB_STATE_CONFLICT", "A scan is already active for this library")
            clustering = db.execute(
                "SELECT id FROM cluster_generations WHERE library_id=? AND status IN ('QUEUED','RUNNING')",
                (library_id,),
            ).fetchone()
            if clustering:
                raise DomainError(
                    "JOB_STATE_CONFLICT", "A cluster generation is active for this library"
                )
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
                "INSERT INTO scan_generations(id,library_id,generation,mode,status,limit_count,root_ids_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    scan_id,
                    library_id,
                    generation,
                    mode,
                    "QUEUED",
                    limit,
                    json.dumps(selected_root_ids),
                    timestamp,
                    timestamp,
                ),
            )
            db.executemany(
                "INSERT INTO scan_roots(scan_id,root_id,status,updated_at) VALUES(?,?,?,?)",
                [(scan_id, root_id, "QUEUED", timestamp) for root_id in selected_root_ids],
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
            roots = [
                {
                    "rootId": item["root_id"],
                    "status": item["status"],
                    "scanned": int(item["scanned"]),
                    "warnings": int(item["warnings"]),
                    "fullTraversalCompleted": bool(item["full_traversal_completed"]),
                    "message": item["message"],
                }
                for item in db.execute(
                    "SELECT * FROM scan_roots WHERE scan_id=? ORDER BY root_id", (scan_id,)
                )
            ]
            return {
                "id": row["id"],
                "libraryId": row["library_id"],
                "generation": int(row["generation"]),
                "mode": row["mode"],
                "status": row["status"],
                "limit": row["limit_count"],
                "rootIds": json.loads(row["root_ids_json"] or "[]"),
                "roots": roots,
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
        root_id: str | None = None,
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
            if root_id is not None:
                updated = db.execute(
                    "UPDATE scan_roots SET status='RUNNING',scanned=scanned+?,warnings=warnings+?,message=?,updated_at=? "
                    "WHERE scan_id=? AND root_id=?",
                    (
                        processed,
                        int(warning) if warning_count is None else warning_count,
                        message,
                        now_iso(),
                        scan_id,
                        root_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise DomainError("VALIDATION_FAILED", "Root is not part of this scan")
            self._transition_job_db(db, scan_id, "RUNNING", min(0.99, scanned / max(scanned + 1, 1)), message)

    def finish_scan_root(
        self,
        scan_id: str,
        root_id: str,
        status: str,
        *,
        full_traversal_completed: bool = False,
        message: str | None = None,
    ) -> None:
        if status not in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
            raise DomainError("VALIDATION_FAILED", "Invalid scan-root terminal state")
        with self._lock, self.connect() as db:
            updated = db.execute(
                "UPDATE scan_roots SET status=?,full_traversal_completed=?,message=?,updated_at=? "
                "WHERE scan_id=? AND root_id=?",
                (
                    status,
                    int(full_traversal_completed),
                    message,
                    now_iso(),
                    scan_id,
                    root_id,
                ),
            )
            if updated.rowcount != 1:
                raise DomainError("NOT_FOUND", "Scan root not found")

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
            scan = db.execute(
                "SELECT library_id,generation,root_ids_json FROM scan_generations WHERE id=?",
                (scan_id,),
            ).fetchone()
            if not scan:
                raise DomainError("NOT_FOUND", "Scan not found")
            policy = db.execute(
                "SELECT time_zone,dst_fold,nonexistent_policy FROM libraries WHERE id=?",
                (scan["library_id"],),
            ).fetchone()
            selected_root_ids = json.loads(scan["root_ids_json"] or "[]")
            validated_roots: dict[str, tuple[sqlite3.Row, Path]] = {}
            for record in records:
                root_id = record.root_id or (selected_root_ids[0] if len(selected_root_ids) == 1 else None)
                if root_id is None or root_id not in selected_root_ids:
                    raise DomainError("VALIDATION_FAILED", "Media record does not identify a selected scan root")
                validated = validated_roots.get(root_id)
                if validated is None:
                    root_row = db.execute(
                        "SELECT roots.root,roots.grant_id,roots.active,roots.time_policy_json,"
                        "grants.root AS grant_root,grants.device,grants.inode,grants.revoked "
                        "FROM library_roots roots JOIN directory_grants grants ON grants.id=roots.grant_id "
                        "WHERE roots.id=? AND roots.library_id=?",
                        (root_id, scan["library_id"]),
                    ).fetchone()
                    if (
                        not root_row
                        or not root_row["active"]
                        or root_row["revoked"]
                        or root_row["root"] != root_row["grant_root"]
                    ):
                        raise DomainError("GRANT_REQUIRED", "Media record root grant is unavailable")
                    validated = (root_row, self._validated_grant_root(root_row))
                    validated_roots[root_id] = validated
                root_row, library_root = validated
                record.root_id = root_id
                storage_path = _storage_relative_path(root_id, record.relative_path)
                effective_policy = dict(policy)
                if root_row["time_policy_json"]:
                    override = json.loads(root_row["time_policy_json"])
                    effective_policy = {
                        "time_zone": override["timeZone"],
                        "dst_fold": int(override["dstFold"]),
                        "nonexistent_policy": override["nonexistentPolicy"],
                    }
                _normalize_media_record_timestamp(record, effective_policy)
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
                            "JOIN media ON media.id=keys.asset_id WHERE keys.library_id=? "
                            "AND media.root_id=? AND keys.identity_key=?",
                            (scan["library_id"], root_id, identity_key),
                        )
                    )
                    rename_matches = []
                    for item in rename_candidates:
                        previous = db.execute(
                            "SELECT record_json FROM media WHERE id=?", (item["asset_id"],)
                        ).fetchone()
                        previous_relative = json.loads(previous["record_json"])["relative_path"]
                        previous_path = library_root / previous_relative
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
                    "ON keys.asset_id=media.id WHERE media.library_id=? AND media.root_id=? "
                    "AND media.relative_path=?",
                    (scan["library_id"], root_id, storage_path),
                ).fetchone()
                if target_owner and identity_key and target_owner["identity_key"] == identity_key:
                    record.id = target_owner["id"]
                existing_asset = db.execute(
                    "SELECT id,relative_path,record_json FROM media "
                    "WHERE library_id=? AND root_id=? AND relative_path=? AND id<>?",
                    (scan["library_id"], root_id, storage_path, record.id),
                ).fetchone()
                if existing_asset:
                    moving = db.execute(
                        "SELECT relative_path,record_json,root_id FROM media WHERE id=?", (record.id,)
                    ).fetchone()
                    replacement_relative = f".__unavailable__/{existing_asset['id']}"
                    replacement_path = _storage_relative_path(root_id, replacement_relative)
                    replacement_missing = 1
                    if moving and moving["root_id"] == root_id:
                        moving_payload = json.loads(moving["record_json"])
                        old_path = library_root / moving_payload["relative_path"]
                        try:
                            old_identity = _identity_key_from_fingerprint(quick_fingerprint(old_path))
                        except OSError:
                            old_identity = None
                        owner_identity_row = db.execute(
                            "SELECT identity_key FROM media_identity_keys WHERE asset_id=?",
                            (existing_asset["id"],),
                        ).fetchone()
                        if owner_identity_row and old_identity == owner_identity_row["identity_key"]:
                            replacement_relative = moving_payload["relative_path"]
                            replacement_path = moving["relative_path"]
                            replacement_missing = 0
                        db.execute(
                            "UPDATE media SET relative_path=? WHERE id=?",
                            (_storage_relative_path(root_id, f".__moving__/{record.id}-{scan_id}"), record.id),
                        )
                    owner_payload = json.loads(existing_asset["record_json"])
                    owner_payload["relative_path"] = replacement_relative
                    owner_payload["missing"] = bool(replacement_missing)
                    db.execute(
                        "UPDATE media SET relative_path=?,missing=?,record_json=? WHERE id=?",
                        (replacement_path, replacement_missing, json.dumps(owner_payload), existing_asset["id"]),
                    )
                payload = record.to_dict()
                payload["generation"] = int(scan["generation"])
                payload["missing"] = False
                db.execute(
                    "INSERT INTO media(id,library_id,root_id,relative_path,captured_at,camera,duration,first_generation,last_generation,missing,fingerprint_json,record_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET root_id=excluded.root_id,relative_path=excluded.relative_path,"
                    "captured_at=excluded.captured_at,camera=excluded.camera,duration=excluded.duration,last_generation=excluded.last_generation,"
                    "missing=0,fingerprint_json=excluded.fingerprint_json,record_json=excluded.record_json",
                    (
                        record.id,
                        scan["library_id"],
                        root_id,
                        storage_path,
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
        cursor = db.execute(
            "SELECT media.id,media.record_json,roots.time_policy_json FROM media "
            "LEFT JOIN library_roots roots ON roots.id=media.root_id "
            "WHERE media.library_id=? ORDER BY media.id",
            (library_id,),
        )
        while rows := cursor.fetchmany(500):
            for row in rows:
                effective_zone = time_zone
                effective_fold = dst_fold
                effective_nonexistent = nonexistent_policy
                if row["time_policy_json"]:
                    override = json.loads(row["time_policy_json"])
                    effective_zone = override["timeZone"]
                    effective_fold = int(override["dstFold"])
                    effective_nonexistent = override["nonexistentPolicy"]
                self._renormalize_timestamp_row_db(
                    db,
                    row,
                    library_id,
                    effective_zone,
                    effective_fold,
                    effective_nonexistent,
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
            if status == "SUCCEEDED":
                db.execute(
                    "UPDATE scan_roots SET status='SUCCEEDED',full_traversal_completed=?,updated_at=? "
                    "WHERE scan_id=? AND status IN ('QUEUED','RUNNING')",
                    (int(scan["mode"] == "FULL"), now_iso(), scan_id),
                )
            if status == "SUCCEEDED" and scan["mode"] == "FULL":
                completed = [
                    item["root_id"]
                    for item in db.execute(
                        "SELECT root_id FROM scan_roots WHERE scan_id=? AND status='SUCCEEDED' "
                        "AND full_traversal_completed=1",
                        (scan_id,),
                    )
                ]
                if completed:
                    db.execute(
                        f"UPDATE media SET missing=1 WHERE library_id=? AND root_id IN "
                        f"({','.join('?' for _ in completed)}) AND last_generation<?",
                        (scan["library_id"], *completed, scan["generation"]),
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
                self._advance_catalog_revision_db(db, scan["library_id"], scan_id)
                db.execute(
                    "UPDATE library_roots SET last_scan_at=? WHERE id IN "
                    "(SELECT root_id FROM scan_roots WHERE scan_id=? AND status='SUCCEEDED')",
                    (now_iso(), scan_id),
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

    def existing_media_by_path(
        self, library_id: str, relative_path: str, root_id: str | None = None
    ) -> dict[str, Any] | None:
        if root_id is None:
            roots = self.active_library_root_paths(library_id)
            if len(roots) != 1:
                raise DomainError("VALIDATION_FAILED", "rootId is required for a multi-root library")
            root_id = roots[0][0]
        return self.existing_media_by_paths(library_id, root_id, [relative_path]).get(relative_path)

    def existing_media_by_paths(
        self, library_id: str, root_id: str, relative_paths: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        paths = list(dict.fromkeys(str(item) for item in relative_paths))
        if not paths:
            return {}
        storage_paths = [_storage_relative_path(root_id, item) for item in paths]
        with self.connect() as db:
            rows = db.execute(
                f"SELECT record_json FROM media WHERE library_id=? AND root_id=? AND relative_path IN "
                f"({','.join('?' for _ in storage_paths)})",
                (library_id, root_id, *storage_paths),
            )
            result = {}
            for row in rows:
                payload = json.loads(row["record_json"])
                result[str(payload["relative_path"])] = payload
            return result

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
        with self.connect() as db:
            return self._media_records_db(db, ids)

    @staticmethod
    def _media_records_db(
        db: sqlite3.Connection, media_ids: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(media_ids))
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), 400):
            batch = ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            for row in db.execute(
                f"SELECT id,record_json,missing FROM media WHERE id IN ({placeholders})", batch
            ):
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
                db.execute(
                    "UPDATE library_roots SET library_id=? WHERE library_id=?",
                    (summary.library_id, library["id"]),
                )
                db.execute("UPDATE media SET library_id=? WHERE library_id=?", (summary.library_id, library["id"]))
                db.execute(
                    "UPDATE media_identity_keys SET library_id=? WHERE library_id=?",
                    (summary.library_id, library["id"]),
                )
                db.execute("UPDATE scan_generations SET library_id=? WHERE library_id=?", (summary.library_id, library["id"]))
                db.execute("UPDATE jobs SET library_id=? WHERE library_id=?", (summary.library_id, library["id"]))
                db.execute("PRAGMA foreign_keys=ON")

    # Projects and commands

    def create_project(
        self,
        name: str,
        library_id: str,
        asset_ids: list[str],
        source_groups: list[dict[str, Any]] | None = None,
        *,
        initialize_legacy_program: bool = False,
    ) -> dict[str, Any]:
        self.library_root(library_id)
        assets = self.media_records(asset_ids)
        if len(assets) != len(set(asset_ids)):
            raise DomainError("NOT_FOUND", "One or more selected media assets are unavailable")
        if any(asset.get("library_id") != library_id for asset in assets.values()):
            raise DomainError("VALIDATION_FAILED", "Every selected media asset must belong to the project library")
        project = new_project(
            name,
            library_id,
            [assets[item] for item in asset_ids],
            source_groups=source_groups,
            initialize_legacy_program=initialize_legacy_program,
        )
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
            self._sync_project_component_delta_db(db, {}, project, int(project["revision"]))
        return self.project(project["id"])

    def create_project_from_selection(
        self,
        name: str,
        library_id: str,
        cluster_generation_id: str,
        session_ids: list[str],
        event_ids: list[str],
        include_asset_ids: list[str],
        exclude_asset_ids: list[str],
    ) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            selected_ids, selection_snapshot = self._resolve_project_selection_db(
                db,
                library_id,
                cluster_generation_id,
                session_ids,
                event_ids,
                include_asset_ids,
                exclude_asset_ids,
            )
            assets = self._media_records_db(db, selected_ids)
            if len(assets) != len(selected_ids):
                raise DomainError("SOURCE_MISSING", "Selected cluster membership is no longer available")
            project = new_project(
                name,
                library_id,
                [assets[item] for item in selected_ids],
                selection_snapshot=selection_snapshot,
                initialize_legacy_program=False,
            )
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
                "INSERT INTO project_revisions(project_id,revision,document_json,created_at) "
                "VALUES(?,?,?,?)",
                (project["id"], project["revision"], json.dumps(project), project["createdAt"]),
            )
            self._sync_project_component_delta_db(db, {}, project, int(project["revision"]))
        return self.project(project["id"])

    def project_selection_preview(
        self,
        library_id: str,
        cluster_generation_id: str,
        session_ids: list[str],
        event_ids: list[str],
        include_asset_ids: list[str],
        exclude_asset_ids: list[str],
    ) -> dict[str, Any]:
        with self._lock, self.connect() as db:
            selected_ids, snapshot = self._resolve_project_selection_db(
                db,
                library_id,
                cluster_generation_id,
                session_ids,
                event_ids,
                include_asset_ids,
                exclude_asset_ids,
            )
            summary = db.execute(
                "SELECT COUNT(*) AS asset_count,"
                "SUM(CASE WHEN media.missing=1 THEN 1 ELSE 0 END) AS unavailable_count,"
                "SUM(CASE WHEN media.captured_at IS NULL THEN 1 ELSE 0 END) AS unresolved_count,"
                "COUNT(DISTINCT media.root_id) AS root_count,"
                "COUNT(DISTINCT COALESCE(members.source_candidate_id,"
                "json_extract(media.record_json,'$.sourceCandidateId'))) AS source_count,"
                "MIN(members.start_us) AS start_us,MAX(members.end_us) AS end_us,"
                "SUM(CASE WHEN members.warnings_json IS NOT NULL AND members.warnings_json!='[]' "
                "THEN 1 ELSE 0 END) AS warning_count "
                "FROM selected_asset_ids selected JOIN media ON media.id=selected.asset_id "
                "LEFT JOIN cluster_memberships members ON members.generation_id=? "
                "AND members.asset_id=selected.asset_id",
                (cluster_generation_id,),
            ).fetchone()
            roots = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT media.root_id FROM selected_asset_ids selected "
                    "JOIN media ON media.id=selected.asset_id WHERE media.root_id IS NOT NULL "
                    "ORDER BY media.root_id"
                )
            ]
        start_us = int(summary["start_us"]) if summary["start_us"] is not None else None
        end_us = int(summary["end_us"]) if summary["end_us"] is not None else None
        span_us = max(0, end_us - start_us) if start_us is not None and end_us is not None else 0
        return {
            "clusterGenerationId": cluster_generation_id,
            "selectionDigest": snapshot["digest"],
            "exactAssetCount": len(selected_ids),
            "evidenceStartUs": start_us,
            "evidenceEndUs": end_us,
            "evidenceSpanUs": span_us,
            "estimatedOutputSpanUs": span_us,
            "rootIds": roots,
            "rootCount": int(summary["root_count"] or 0),
            "sourceCandidateCount": int(summary["source_count"] or 0),
            "unresolvedTimestampCount": int(summary["unresolved_count"] or 0),
            "unavailableAssetCount": int(summary["unavailable_count"] or 0),
            "warningAssetCount": int(summary["warning_count"] or 0),
        }

    def _resolve_project_selection_db(
        self,
        db: sqlite3.Connection,
        library_id: str,
        cluster_generation_id: str,
        session_ids: list[str],
        event_ids: list[str],
        include_asset_ids: list[str],
        exclude_asset_ids: list[str],
    ) -> tuple[list[str], dict[str, Any]]:
        sessions = list(dict.fromkeys(session_ids))
        events = list(dict.fromkeys(event_ids))
        includes = list(dict.fromkeys(include_asset_ids))
        excludes = list(dict.fromkeys(exclude_asset_ids))
        if set(includes) & set(excludes):
            raise DomainError(
                "VALIDATION_FAILED", "The same asset cannot be both manually included and excluded"
            )
        generation = db.execute(
            "SELECT * FROM cluster_generations WHERE id=? AND library_id=?",
            (cluster_generation_id, library_id),
        ).fetchone()
        if not generation:
            raise DomainError("NOT_FOUND", "Cluster generation not found for library")
        if generation["status"] != "SUCCEEDED":
            raise DomainError("JOB_STATE_CONFLICT", "Cluster generation is not complete")
        db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS selected_cluster_ids("
            "kind TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(kind,id))"
        )
        db.execute("DELETE FROM selected_cluster_ids")
        db.executemany(
            "INSERT INTO selected_cluster_ids(kind,id) VALUES('SESSION',?)",
            ((item,) for item in sessions),
        )
        db.executemany(
            "INSERT INTO selected_cluster_ids(kind,id) VALUES('EVENT',?)",
            ((item,) for item in events),
        )
        known_sessions = int(
            db.execute(
                "SELECT COUNT(*) FROM session_clusters clusters "
                "JOIN selected_cluster_ids selected ON selected.kind='SESSION' "
                "AND selected.id=clusters.id WHERE clusters.generation_id=?",
                (cluster_generation_id,),
            ).fetchone()[0]
        )
        known_events = int(
            db.execute(
                "SELECT COUNT(*) FROM event_clusters clusters "
                "JOIN selected_cluster_ids selected ON selected.kind='EVENT' "
                "AND selected.id=clusters.id WHERE clusters.generation_id=?",
                (cluster_generation_id,),
            ).fetchone()[0]
        )
        if known_sessions != len(sessions) or known_events != len(events):
            raise DomainError(
                "VALIDATION_FAILED", "Every selected session and event must belong to the generation"
            )
        membership_rows = list(
            db.execute(
                "SELECT members.asset_id,MIN(members.start_us) AS start_us "
                "FROM cluster_memberships members WHERE members.generation_id=? AND ("
                "EXISTS(SELECT 1 FROM selected_cluster_ids selected WHERE selected.kind='SESSION' "
                "AND selected.id=members.session_id) OR "
                "EXISTS(SELECT 1 FROM selected_cluster_ids selected WHERE selected.kind='EVENT' "
                "AND selected.id=members.event_id)) GROUP BY members.asset_id "
                "ORDER BY start_us,members.asset_id",
                (cluster_generation_id,),
            )
        )
        cluster_asset_ids = [row["asset_id"] for row in membership_rows]
        adjustments = self._media_records_db(db, [*includes, *excludes])
        if len(adjustments) != len(set([*includes, *excludes])):
            raise DomainError("NOT_FOUND", "One or more manual media adjustments are unavailable")
        if any(item.get("library_id") != library_id for item in adjustments.values()):
            raise DomainError(
                "VALIDATION_FAILED", "Every manual media adjustment must belong to the library"
            )
        selected_ids = list(dict.fromkeys([*cluster_asset_ids, *includes]))
        excluded = set(excludes)
        selected_ids = [asset_id for asset_id in selected_ids if asset_id not in excluded]
        if not selected_ids:
            raise DomainError("VALIDATION_FAILED", "Project selection contains no media assets")
        assets = self._media_records_db(db, selected_ids)
        if len(assets) != len(selected_ids):
            raise DomainError("SOURCE_MISSING", "Selected cluster membership is no longer available")
        if any(item.get("library_id") != library_id for item in assets.values()):
            raise DomainError(
                "VALIDATION_FAILED", "Every selected media asset must belong to the project library"
            )
        db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS selected_asset_ids("
            "position INTEGER NOT NULL,asset_id TEXT PRIMARY KEY)"
        )
        db.execute("DELETE FROM selected_asset_ids")
        db.executemany(
            "INSERT INTO selected_asset_ids(position,asset_id) VALUES(?,?)",
            enumerate(selected_ids),
        )
        selection_snapshot = {
            "clusterGenerationId": cluster_generation_id,
            "selectedSessionIds": sessions,
            "selectedEventIds": events,
            "assetIds": selected_ids,
            "manualIncludeAssetIds": includes,
            "manualExcludeAssetIds": excludes,
        }
        selection_snapshot["digest"] = digest_json(selection_snapshot)
        return selected_ids, selection_snapshot

    def save_project(self, project: dict[str, Any]) -> None:
        canonical = self._migrate_legacy_project(project)
        with self._lock, self.connect() as db:
            previous_row = db.execute(
                "SELECT document_json FROM projects WHERE id=?", (canonical["id"],)
            ).fetchone()
            previous = (
                self._migrate_legacy_project(json.loads(previous_row["document_json"]))
                if previous_row
                else {}
            )
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
            self._sync_project_component_delta_db(
                db, previous, canonical, int(canonical["revision"])
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
            if row:
                return self._migrate_legacy_project(json.loads(row[0]))
            snapshot = db.execute(
                "SELECT revision,document_json FROM project_revisions WHERE project_id=? AND revision<? "
                "ORDER BY revision DESC LIMIT 1",
                (project_id, int(revision)),
            ).fetchone()
            if not snapshot:
                raise DomainError("NOT_FOUND", "Retained project revision not found")
            project = self._migrate_legacy_project(json.loads(snapshot["document_json"]))
            expected = int(snapshot["revision"]) + 1
            for delta_row in db.execute(
                "SELECT revision,delta_json FROM project_revision_deltas WHERE project_id=? "
                "AND revision>? AND revision<=? ORDER BY revision",
                (project_id, int(snapshot["revision"]), int(revision)),
            ):
                if int(delta_row["revision"]) != expected:
                    raise DomainError("NOT_FOUND", "Retained project revision chain is incomplete")
                project = _apply_project_delta(project, json.loads(delta_row["delta_json"]))
                expected += 1
            if expected != int(revision) + 1:
                raise DomainError("NOT_FOUND", "Retained project revision chain is incomplete")
            return self._migrate_legacy_project(project)

    def apply_project_command(
        self,
        project_id: str,
        envelope: dict[str, Any],
        preview: bool = False,
        *,
        delta_result: bool = False,
    ) -> dict[str, Any]:
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
            if command_type in {"AcceptAlignmentSuggestion", "AcceptAlignmentSuggestions"}:
                self._validate_alignment_acceptance_db(
                    db, project_id, current_revision, command_type, payload
                )
            proposal_action: dict[str, Any] | None = None
            if command_type in {
                "AcceptAlignmentProposalSet",
                "AcceptAlignmentProposal",
                "RejectAlignmentProposal",
                "RejectAlignmentProposalSet",
            }:
                payload, proposal_action = self._prepare_alignment_proposal_command_db(
                    db, project_id, current_revision, command_type, payload
                )
            assets = self._media_records_db(db, [item["assetId"] for item in project.get("clips", [])])
            previous_compiled = compile_program(project, assets) if command_type in PROGRAM_AFFECTING_COMMANDS else None
            changed = apply_command(project, command_type, payload, assets)
            changed["revision"] = current_revision + 1
            changed["updatedAt"] = now_iso()
            compiled = compile_program(changed, assets)
            full_result = {
                "commandId": command_id,
                "projectId": project_id,
                "previousRevision": current_revision,
                "appliedRevision": changed["revision"],
                "project": changed,
                "issues": compiled["issues"],
                "preparation": project_preparation(changed, assets),
                "reviewState": "STALE" if project.get("review") else "NOT_REVIEWED",
                "eventCursor": self.latest_event_sequence(db),
                "preview": preview,
                "affectedIntervals": _affected_program_intervals(previous_compiled, compiled) if previous_compiled else [],
            }
            result = (
                _delta_command_result(project, changed, full_result, previous_compiled, compiled)
                if delta_result
                else full_result
            )
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
            delta = _project_delta(project, changed)
            db.execute(
                "INSERT INTO project_revision_deltas(project_id,revision,base_revision,delta_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    project_id,
                    changed["revision"],
                    current_revision,
                    json.dumps(delta),
                    changed["updatedAt"],
                ),
            )
            if (
                int(changed["revision"]) % PROJECT_SNAPSHOT_INTERVAL == 0
                or command_type == "GenerateProgramDraft"
            ):
                db.execute(
                    "INSERT INTO project_revisions(project_id,revision,document_json,created_at) VALUES(?,?,?,?)",
                    (project_id, changed["revision"], json.dumps(changed), changed["updatedAt"]),
                )
            self._sync_project_component_delta_db(
                db, project, changed, int(changed["revision"])
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
            if proposal_action:
                self._set_alignment_proposal_status_db(
                    db,
                    proposal_action["proposalSetId"],
                    proposal_action["status"],
                    proposal_action.get("acceptedProposalIds", []),
                    proposal_action.get("rejectedProposalIds", []),
                )
            self._invalidate_alignment_proposal_sets_db(
                db,
                project_id,
                changed["revision"],
                exclude_id=proposal_action["proposalSetId"] if proposal_action else None,
            )
            if command_type in {"AcceptAlignmentSuggestion", "RejectAlignmentSuggestion"}:
                suggestion_id = str(payload.get("suggestionId", ""))
                next_status = "ACCEPTED" if command_type == "AcceptAlignmentSuggestion" else "REJECTED"
                self._set_suggestion_status_db(db, suggestion_id, next_status)
            elif command_type == "AcceptAlignmentSuggestions":
                for suggestion in payload.get("suggestions", []):
                    self._set_suggestion_status_db(db, str(suggestion.get("suggestionId", "")), "ACCEPTED")
            return result

    def apply_project_delta_command(
        self, project_id: str, envelope: dict[str, Any], preview: bool = False
    ) -> dict[str, Any]:
        return self.apply_project_command(
            project_id, envelope, preview, delta_result=True
        )

    def _prepare_alignment_proposal_command_db(
        self,
        db: sqlite3.Connection,
        project_id: str,
        project_revision: int,
        command_type: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        proposal_set_id = str(payload.get("proposalSetId", ""))
        row = db.execute(
            "SELECT * FROM alignment_proposal_sets WHERE id=?", (proposal_set_id,)
        ).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Alignment proposal set not found")
        value = json.loads(row["set_json"])
        if row["project_id"] != project_id:
            raise DomainError("VALIDATION_FAILED", "Alignment proposal set belongs to another project")
        if int(row["project_revision"]) > project_revision or row["status"] not in {
            "PENDING",
            "PARTIALLY_RESOLVED",
        }:
            raise DomainError("PLAN_STALE", "Alignment proposal set is no longer current")
        if str(payload.get("digest", "")) != str(row["proposal_digest"]):
            raise DomainError("PLAN_STALE", "Alignment proposal set digest does not match")
        proposals = {str(item["id"]): item for item in value.get("proposals", [])}
        already_resolved = set(value.get("acceptedProposalIds", [])) | set(
            value.get("rejectedProposalIds", [])
        )
        if command_type == "RejectAlignmentProposalSet":
            return dict(payload), {
                "proposalSetId": proposal_set_id,
                "status": "REJECTED",
                "rejectedProposalIds": sorted(proposals),
            }
        if command_type == "RejectAlignmentProposal":
            proposal_id = str(payload.get("proposalId", ""))
            if proposal_id not in proposals:
                raise DomainError("NOT_FOUND", "Alignment proposal not found in set")
            return dict(payload), {
                "proposalSetId": proposal_set_id,
                "status": "PARTIALLY_RESOLVED",
                "rejectedProposalIds": [proposal_id],
            }
        selected: list[dict[str, Any]]
        if command_type == "AcceptAlignmentProposalSet":
            mode = str(payload.get("mode", "HIGH_CONFIDENCE"))
            if mode not in {"HIGH_CONFIDENCE", "TIMESTAMP_PRIOR"}:
                raise DomainError("VALIDATION_FAILED", "Unknown proposal-set acceptance mode")
            original_scope = payload.get("scope") or {"kind": "PROJECT"}
            project_row = db.execute(
                "SELECT document_json FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            current_project = self._migrate_legacy_project(json.loads(project_row["document_json"]))
            selection_scope = self._normalized_acceptance_scope_db(
                db, current_project, original_scope
            )
            selected = self._select_alignment_proposals(
                value, mode, selection_scope, already_resolved
            )
            if mode == "TIMESTAMP_PRIOR":
                accepted_clip_ids = {
                    str(clip["id"])
                    for clip in current_project.get("clips", [])
                    if clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED")
                    == "ACCEPTED"
                }
                selected = [item for item in selected if str(item["clipId"]) not in accepted_clip_ids]
                if not payload.get("confirmTimestampUncertainty"):
                    raise DomainError(
                        "TIMESTAMP_CONFIRMATION_REQUIRED",
                        "Timestamp-prior acceptance requires explicit confirmation",
                    )
                preview = db.execute(
                    "SELECT * FROM alignment_acceptance_previews WHERE id=?",
                    (str(payload.get("previewId", "")),),
                ).fetchone()
                if (
                    not preview
                    or preview["project_id"] != project_id
                    or int(preview["project_revision"]) != project_revision
                    or preview["proposal_set_id"] != proposal_set_id
                    or preview["proposal_digest"] != row["proposal_digest"]
                    or preview["preview_digest"] != str(payload.get("previewDigest", ""))
                    or float(preview["expires_at"]) <= time.time()
                ):
                    raise DomainError(
                        "ACCEPTANCE_PREVIEW_STALE", "Alignment acceptance preview is stale"
                    )
                canonical_preview = json.loads(preview["preview_json"])
                if canonical_preview.get("mode") != mode:
                    raise DomainError(
                        "ACCEPTANCE_PREVIEW_STALE",
                        "Acceptance mode changed after preview",
                    )
                if canonical_preview.get("scopeDigest") != digest_json(payload.get("scope") or {"kind": "PROJECT"}):
                    raise DomainError("ACCEPTANCE_PREVIEW_STALE", "Acceptance scope changed after preview")
                if sorted(item["id"] for item in selected) != sorted(canonical_preview["proposalIds"]):
                    raise DomainError("ACCEPTANCE_PREVIEW_STALE", "Applicable proposals changed after preview")
        else:
            proposal_id = str(payload.get("proposalId", ""))
            proposal = proposals.get(proposal_id)
            if not proposal:
                raise DomainError("NOT_FOUND", "Alignment proposal not found in set")
            if proposal_id in already_resolved:
                raise DomainError("JOB_STATE_CONFLICT", "Alignment proposal was already resolved")
            if not proposal.get("automaticallyAcceptable") and not payload.get("confirmLowConfidence"):
                raise DomainError(
                    "VALIDATION_FAILED", "Low-confidence alignment requires explicit confirmation"
                )
            selected = [proposal]
        if not selected:
            raise DomainError("VALIDATION_FAILED", "Proposal set has no applicable alignments")
        if any(item.get("requiresDriftConfirmation") for item in selected) and not payload.get("confirmDrift"):
            raise DomainError("VALIDATION_FAILED", "Proposed drift requires explicit confirmation")
        expanded = dict(payload)
        expanded["alignments"] = [
            {
                "proposalId": item["id"],
                "clipId": item["clipId"],
                "alignment": item["proposedAlignment"],
                "confidence": item["confidence"],
                "evidenceKinds": (
                    ["audio-correlation", "timestamp-prior"]
                    if item.get("classification") == "AUDIO_CONFIRMED"
                    else ["timestamp-prior"]
                    if item.get("classification") == "TIMESTAMP_ONLY"
                    else ["manual-review"]
                ),
            }
            for item in selected
        ]
        return expanded, {
            "proposalSetId": proposal_set_id,
            "status": "PARTIALLY_RESOLVED",
            "acceptedProposalIds": [item["id"] for item in selected],
        }

    @staticmethod
    def _select_alignment_proposals(
        proposal_set: dict[str, Any],
        mode: str,
        scope: dict[str, Any],
        already_resolved: set[str],
    ) -> list[dict[str, Any]]:
        kind = str(scope.get("kind", "PROJECT"))
        if kind not in {"PROJECT", "EVENTS", "SOURCES", "ALIGNED_RANGE", "CLIPS"}:
            raise DomainError("SCOPE_INVALID", "Unknown alignment acceptance scope")
        for field in ("clipIds", "sourceIds", "eventIds"):
            if field in scope and not isinstance(scope[field], list):
                raise DomainError("SCOPE_INVALID", f"{field} must be an array")
        clip_ids = {str(value) for value in scope.get("clipIds", [])}
        source_ids = {str(value) for value in scope.get("sourceIds", [])}
        event_ids = {str(value) for value in scope.get("eventIds", [])}
        try:
            start_us = int(scope.get("startAlignedUs", -2**63))
            end_us = int(scope.get("endAlignedUs", 2**63 - 1))
        except (TypeError, ValueError) as error:
            raise DomainError(
                "SCOPE_INVALID", "Aligned range bounds must be integers"
            ) from error
        if kind == "ALIGNED_RANGE" and end_us <= start_us:
            raise DomainError("SCOPE_INVALID", "Aligned acceptance range must have positive duration")

        def in_scope(item: dict[str, Any]) -> bool:
            if kind == "CLIPS":
                return str(item.get("clipId")) in clip_ids
            if kind == "SOURCES":
                return str(item.get("logicalSourceId")) in source_ids
            if kind == "EVENTS":
                return str(item.get("eventId")) in event_ids
            if kind == "ALIGNED_RANGE":
                transform = ClipAlignmentTransform.from_dict(item.get("proposedAlignment"))
                proposed_start = transform.source_to_aligned(0)
                proposed_end = int(item.get("proposedEndAlignedUs") or proposed_start + 1)
                return proposed_start < end_us and proposed_end > start_us
            return True

        return [
            item
            for item in proposal_set.get("proposals", [])
            if item.get("id") not in already_resolved
            and in_scope(item)
            and (
                bool(item.get("automaticallyAcceptable"))
                if mode == "HIGH_CONFIDENCE"
                else bool(
                    item.get(
                        "timestampPriorAcceptable",
                        item.get("classification") == "TIMESTAMP_ONLY",
                    )
                )
                and not item.get("requiresDriftConfirmation")
            )
        ]

    @staticmethod
    def _normalized_acceptance_scope_db(
        db: sqlite3.Connection, project: dict[str, Any], scope: dict[str, Any]
    ) -> dict[str, Any]:
        if str(scope.get("kind", "PROJECT")) != "EVENTS":
            return scope
        event_ids = [str(value) for value in scope.get("eventIds", [])]
        generation_id = (project.get("selectionSnapshot") or {}).get("clusterGenerationId")
        if not event_ids or not generation_id:
            raise DomainError("SCOPE_INVALID", "Event scope requires the project's cluster generation")
        placeholders = ",".join("?" for _item in event_ids)
        asset_ids = {
            str(row[0])
            for row in db.execute(
                f"SELECT asset_id FROM cluster_memberships WHERE generation_id=? "
                f"AND event_id IN ({placeholders})",
                (generation_id, *event_ids),
            )
        }
        clip_ids = [
            str(clip["id"]) for clip in project.get("clips", []) if str(clip["assetId"]) in asset_ids
        ]
        return {"kind": "CLIPS", "clipIds": clip_ids}

    def _validate_alignment_acceptance_db(
        self,
        db: sqlite3.Connection,
        project_id: str,
        project_revision: int,
        command_type: str,
        payload: dict[str, Any],
    ) -> None:
        requested = (
            [payload]
            if command_type == "AcceptAlignmentSuggestion"
            else payload.get("suggestions", [])
        )
        if not isinstance(requested, list) or not requested:
            raise DomainError("VALIDATION_FAILED", "At least one alignment suggestion is required")
        for item in requested:
            if not isinstance(item, dict):
                raise DomainError("VALIDATION_FAILED", "Every accepted suggestion must be an object")
            row = db.execute(
                "SELECT project_id,status,project_revision,suggestion_json FROM suggestions WHERE id=?",
                (str(item.get("suggestionId", "")),),
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Suggestion not found")
            canonical = json.loads(row["suggestion_json"])
            if (
                row["project_id"] != project_id
                or row["status"] != "PENDING"
                or int(row["project_revision"] or -1) != project_revision
            ):
                raise DomainError("VALIDATION_FAILED", "Suggestion is not pending for this project revision")
            if item.get("clipId") != canonical.get("clipId") or item.get("sync") != canonical.get("sync"):
                raise DomainError("VALIDATION_FAILED", "Suggestion payload does not match canonical evidence")

    def media_source_path(self, media_id: str) -> Path:
        with self.connect() as db:
            media = db.execute(
                "SELECT id,library_id,root_id,relative_path,missing FROM media WHERE id=?",
                (media_id,),
            ).fetchone()
        if not media:
            raise DomainError("NOT_FOUND", f"Unknown media: {media_id}")
        if media["missing"]:
            raise DomainError("SOURCE_MISSING", "Source media is missing")
        try:
            root_id = media["root_id"]
            if root_id:
                with self.connect() as db:
                    row = db.execute(
                        "SELECT roots.root,roots.active,grants.root AS grant_root,grants.device,"
                        "grants.inode,grants.revoked "
                        "FROM library_roots roots JOIN directory_grants grants ON grants.id=roots.grant_id "
                        "WHERE roots.id=? AND roots.library_id=?",
                        (root_id, media["library_id"]),
                    ).fetchone()
                if (
                    not row
                    or not row["active"]
                    or row["revoked"]
                    or row["root"] != row["grant_root"]
                ):
                    raise DomainError("GRANT_REQUIRED", "Source folder grant is unavailable")
                root = self._validated_grant_root(row)
            else:
                root = self.library_root(str(media["library_id"])).resolve(strict=True)
            relative_path = str(media["relative_path"])
            storage_prefix = f"{root_id}::" if root_id else ""
            if storage_prefix and relative_path.startswith(storage_prefix):
                relative_path = relative_path[len(storage_prefix) :]
            target = (root / relative_path).resolve(strict=True)
        except FileNotFoundError as error:
            raise DomainError("SOURCE_MISSING", "Source media is missing") from error
        if not target.is_relative_to(root) or not target.is_file():
            raise DomainError("FORBIDDEN", "Source media resolves outside its directory grant")
        return target

    def compiled_project(self, project_id: str) -> dict[str, Any]:
        project = self.project(project_id)
        assets = self.media_records(item["assetId"] for item in project["clips"])
        return compile_program(project, assets)

    def project_alignment_summary(self, project_id: str) -> dict[str, Any]:
        project = self.project(project_id)
        assets = self.media_records(item["assetId"] for item in project.get("clips", []))
        return alignment_summary(project, assets)

    def project_preparation(self, project_id: str) -> dict[str, Any]:
        project = self.project(project_id)
        assets = self.media_records(item["assetId"] for item in project.get("clips", []))
        return project_preparation(project, assets)

    def project_timeline_window(
        self,
        project_id: str,
        start_aligned_us: int,
        end_aligned_us: int,
        resolution_us: int,
        lane_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        project = self.project(project_id)
        assets = self.media_records(item["assetId"] for item in project.get("clips", []))
        return timeline_window(
            project,
            assets,
            start_aligned_us,
            end_aligned_us,
            resolution_us,
            lane_ids,
        )

    def project_timeline_section_proposal(
        self, project_id: str, gap_mode: str = "EXCLUDE"
    ) -> dict[str, Any]:
        project = self.project(project_id)
        assets = self.media_records(item["assetId"] for item in project.get("clips", []))
        return timeline_section_proposal(project, assets, gap_mode)

    def _migrate_legacy_project(self, project: dict[str, Any]) -> dict[str, Any]:
        if "logicalSources" in project:
            canonical = copy.deepcopy(project)
            for source in canonical.get("logicalSources", []):
                # Existing source decisions predate explicit preparation state. Preserve
                # them as accepted instead of turning a migration into a new decision.
                source.setdefault("identityState", "USER_CONFIRMED")
            for clip in canonical.get("clips", []):
                state = clip.get(
                    "alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED"
                )
                clip.setdefault(
                    "programEligibility",
                    "ELIGIBLE" if state == "ACCEPTED" else "HELD_FOR_REVIEW",
                )
            asset_ids = [str(item["assetId"]) for item in canonical.get("clips", [])]
            snapshot = canonical.setdefault(
                "selectionSnapshot",
                {
                    "clusterGenerationId": None,
                    "selectedSessionIds": [],
                    "selectedEventIds": [],
                    "assetIds": asset_ids,
                    "manualIncludeAssetIds": asset_ids,
                    "manualExcludeAssetIds": [],
                },
            )
            snapshot.setdefault("assetIds", asset_ids)
            snapshot["digest"] = digest_json(
                {key: value for key, value in snapshot.items() if key != "digest"}
            )
            canonical.setdefault("timelineSections", [])
            canonical.setdefault("syntheticSlates", [])
            canonical.setdefault(
                "programDraft",
                {
                    "id": _stable_migration_id("draft", canonical.get("id", "legacy")),
                    "selectionDigest": snapshot["digest"],
                    "alignmentDigest": "legacy",
                    "generatedAt": canonical.get("updatedAt", now_iso()),
                    "strategy": "legacy-import",
                }
                if canonical.get("videoBlocks")
                else None,
            )
            canonical["alignmentDigest"] = alignment_digest(canonical)
            return canonical
        media_ids = list((project.get("alignment") or {}).keys())
        media_ids += [
            item["mediaId"]
            for item in project.get("videoSegments", []) + project.get("audioSegments", [])
            if item.get("mediaId")
        ]
        assets = self.media_records(media_ids)
        if not assets:
            return project
        legacy_assets = list(assets.values())
        # A legacy per-media source was an accepted editorial identity. Candidate
        # evidence introduced later may suggest that two files share a camera,
        # but migration must not silently merge those accepted identities.
        canonical = new_project(
            project.get("name", "Migrated project"),
            project["libraryId"],
            legacy_assets,
            project["id"],
            source_groups=[
                {
                    "label": item.get("camera") or f"Source {index + 1}",
                    "assetIds": [item["id"]],
                }
                for index, item in enumerate(legacy_assets)
            ],
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
                self._invalidate_alignment_proposal_sets_db(
                    db, project["id"], int(project["revision"]) + 1
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

    def save_alignment_proposal_set(self, proposal_set: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(proposal_set)
        required = {
            "id",
            "projectId",
            "projectRevision",
            "selectionDigest",
            "inputDigest",
            "digest",
            "algorithm",
            "algorithmVersion",
            "configDigest",
            "status",
            "summary",
            "proposals",
            "createdAt",
            "updatedAt",
        }
        if required - value.keys():
            raise DomainError("VALIDATION_FAILED", "Alignment proposal set is incomplete")
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            project_row = db.execute(
                "SELECT revision,document_json FROM projects WHERE id=?", (value["projectId"],)
            ).fetchone()
            if not project_row:
                raise DomainError("NOT_FOUND", "Project not found")
            current = self._migrate_legacy_project(json.loads(project_row["document_json"]))
            settings_row = db.execute(
                "SELECT overlap_search_extension_us FROM application_settings WHERE singleton=1"
            ).fetchone()
            if (
                int(project_row["revision"]) != int(value["projectRevision"])
                or str((current.get("selectionSnapshot") or {}).get("digest", ""))
                != str(value["selectionDigest"])
            ):
                value["status"] = "STALE"
                value["invalidationReason"] = "Project changed before analysis completed"
                value["updatedAt"] = now_iso()
            elif settings_row and int(
                value.get("config", {}).get("overlapSearchExtensionUs", -1)
            ) != int(settings_row["overlap_search_extension_us"]):
                value["status"] = "STALE"
                value["invalidationReason"] = "Overlap search settings changed during analysis"
                value["updatedAt"] = now_iso()
            rows = list(
                db.execute(
                    "SELECT id,set_json FROM alignment_proposal_sets "
                    "WHERE project_id=? AND status='PENDING'",
                    (value["projectId"],),
                )
            )
            for row in rows:
                previous = json.loads(row["set_json"])
                previous["status"] = "SUPERSEDED"
                previous["invalidationReason"] = "A newer alignment proposal set was created"
                previous["updatedAt"] = now_iso()
                db.execute(
                    "UPDATE alignment_proposal_sets SET status='SUPERSEDED',set_json=?,updated_at=? WHERE id=?",
                    (json.dumps(previous), previous["updatedAt"], row["id"]),
                )
            db.execute(
                "INSERT INTO alignment_proposal_sets(id,project_id,project_revision,selection_digest,"
                "input_digest,proposal_digest,algorithm,algorithm_version,config_digest,status,set_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    value["id"],
                    value["projectId"],
                    int(value["projectRevision"]),
                    value["selectionDigest"],
                    value["inputDigest"],
                    value["digest"],
                    value["algorithm"],
                    value["algorithmVersion"],
                    value["configDigest"],
                    value["status"],
                    json.dumps(value),
                    value["createdAt"],
                    value["updatedAt"],
                ),
            )
        return value

    def alignment_proposal_sets(self, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise DomainError("NOT_FOUND", "Project not found")
            return [
                json.loads(row["set_json"])
                for row in db.execute(
                    "SELECT set_json FROM alignment_proposal_sets WHERE project_id=? "
                    "ORDER BY created_at DESC,id DESC LIMIT ?",
                    (project_id, limit),
                )
            ]

    def alignment_proposal_set(self, proposal_set_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT set_json FROM alignment_proposal_sets WHERE id=?", (proposal_set_id,)
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Alignment proposal set not found")
            return json.loads(row["set_json"])

    def create_alignment_acceptance_preview(
        self, project_id: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        project = self.project(project_id)
        expected_revision = int(request.get("expectedRevision", 0))
        if expected_revision != int(project["revision"]):
            raise DomainError(
                "REVISION_CONFLICT",
                "Project revision changed",
                {"currentRevision": int(project["revision"])},
            )
        proposal_set = self.alignment_proposal_set(str(request.get("proposalSetId", "")))
        if proposal_set.get("projectId") != project_id or proposal_set.get("status") not in {
            "PENDING", "PARTIALLY_RESOLVED"
        }:
            raise DomainError("PROPOSAL_SET_STALE", "Alignment proposal set is stale")
        if str(request.get("proposalSetDigest", "")) != str(proposal_set.get("digest", "")):
            raise DomainError("PROPOSAL_SET_STALE", "Alignment proposal set digest changed")
        mode = str(request.get("mode", "TIMESTAMP_PRIOR"))
        if mode not in {"HIGH_CONFIDENCE", "TIMESTAMP_PRIOR"}:
            raise DomainError("VALIDATION_FAILED", "Unknown acceptance preview mode")
        scope = request.get("scope") or {"kind": "PROJECT"}
        if not isinstance(scope, dict):
            raise DomainError("SCOPE_INVALID", "Acceptance scope must be an object")
        resolved = set(proposal_set.get("acceptedProposalIds", [])) | set(
            proposal_set.get("rejectedProposalIds", [])
        )
        with self.connect() as db:
            selection_scope = self._normalized_acceptance_scope_db(db, project, scope)
        selected = self._select_alignment_proposals(proposal_set, mode, selection_scope, resolved)
        if mode == "TIMESTAMP_PRIOR":
            accepted_clip_ids = {
                str(clip["id"])
                for clip in project.get("clips", [])
                if clip.get("alignmentState", "ACCEPTED" if "sync" in clip else "UNRESOLVED")
                == "ACCEPTED"
            }
            selected = [item for item in selected if str(item["clipId"]) not in accepted_clip_ids]
        if not selected:
            raise DomainError("VALIDATION_FAILED", "No applicable alignment proposals in scope")
        assets = self.media_records(item["assetId"] for item in project.get("clips", []))
        before = alignment_summary(project, assets)
        expanded = {
            "alignments": [
                {
                    "proposalId": item["id"],
                    "clipId": item["clipId"],
                    "alignment": item["proposedAlignment"],
                    "confidence": item["confidence"],
                    "evidenceKinds": ["timestamp-prior"]
                    if item.get("classification") == "TIMESTAMP_ONLY"
                    else ["audio-correlation", "timestamp-prior"],
                }
                for item in selected
            ],
            "confirmDrift": False,
        }
        simulated = apply_command(project, "AcceptAlignmentProposalSet", expanded, assets)
        after = alignment_summary(simulated, assets)
        selected_clip_ids = {str(item["clipId"]) for item in selected}
        ranges = [
            (int(item["startAlignedUs"]), int(item["endAlignedUs"]))
            for item in after.get("coverageIntervals", [])
            if selected_clip_ids.intersection(item.get("acceptedEligibleVideoClipIds", []))
        ]
        ranges = _merge_time_ranges(ranges)
        created_at = now_iso()
        value = {
            "id": opaque_id("alignpreview"),
            "projectId": project_id,
            "projectRevision": int(project["revision"]),
            "proposalSetId": proposal_set["id"],
            "proposalSetDigest": proposal_set["digest"],
            "mode": mode,
            "scope": scope,
            "scopeDigest": digest_json(scope),
            "expiresAt": datetime.fromtimestamp(time.time() + 900, UTC).isoformat(),
            "affectedProposalCount": len(selected),
            "affectedClipCount": len({item["clipId"] for item in selected}),
            "affectedEventCount": len({item.get("eventId") for item in selected if item.get("eventId")}),
            "affectedSourceCount": len({item.get("logicalSourceId") for item in selected if item.get("logicalSourceId")}),
            "affectedAlignedRanges": [
                {"startAlignedUs": start, "endAlignedUs": end} for start, end in ranges
            ],
            "acceptedCoverageBeforeUs": before["coverage"]["acceptedCoverageUs"],
            "acceptedCoverageAfterUs": after["coverage"]["acceptedCoverageUs"],
            "soleCoverageBlockedBeforeUs": before["coverage"]["unresolvedSoleCoverageUs"],
            "soleCoverageBlockedAfterUs": after["coverage"]["unresolvedSoleCoverageUs"],
            "remainingCounts": after["confidenceCounts"],
            "proposedDriftClipIds": [
                item["clipId"] for item in selected if item.get("requiresDriftConfirmation")
            ],
            "resultingReadiness": after["readyForProgramDraft"],
            "remainingBlockerCount": len(after.get("blockers", [])),
            "remainingBlockedUs": after["coverage"]["unresolvedSoleCoverageUs"],
            "warnings": after.get("warnings", []),
            "proposalIds": [item["id"] for item in selected],
            "createdAt": created_at,
        }
        value["digest"] = digest_json(value)
        with self.connect() as db:
            db.execute(
                "INSERT INTO alignment_acceptance_previews(id,project_id,project_revision,proposal_set_id,"
                "proposal_digest,preview_digest,expires_at,preview_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    value["id"], project_id, project["revision"], proposal_set["id"],
                    proposal_set["digest"], value["digest"], time.time() + 900,
                    json.dumps(value), created_at,
                ),
            )
        return value

    def _set_alignment_proposal_status_db(
        self,
        db: sqlite3.Connection,
        proposal_set_id: str,
        status: str,
        accepted_proposal_ids: list[str],
        rejected_proposal_ids: list[str],
    ) -> None:
        row = db.execute(
            "SELECT set_json FROM alignment_proposal_sets WHERE id=?", (proposal_set_id,)
        ).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Alignment proposal set not found")
        value = json.loads(row["set_json"])
        accepted = sorted(
            set(value.get("acceptedProposalIds", [])) | set(accepted_proposal_ids)
        )
        rejected = sorted(
            set(value.get("rejectedProposalIds", [])) | set(rejected_proposal_ids)
        )
        proposal_ids = {str(item["id"]) for item in value.get("proposals", [])}
        if status in {"REJECTED", "STALE", "SUPERSEDED"}:
            resolved_status = status
        elif proposal_ids and proposal_ids <= set(accepted):
            resolved_status = "ACCEPTED"
        elif proposal_ids and proposal_ids <= set(rejected):
            resolved_status = "REJECTED"
        elif proposal_ids and proposal_ids <= set(accepted) | set(rejected):
            resolved_status = "RESOLVED"
        else:
            resolved_status = "PARTIALLY_RESOLVED"
        value["status"] = resolved_status
        value["acceptedProposalIds"] = accepted
        value["rejectedProposalIds"] = rejected
        value["updatedAt"] = now_iso()
        db.execute(
            "UPDATE alignment_proposal_sets SET status=?,set_json=?,updated_at=? WHERE id=?",
            (resolved_status, json.dumps(value), value["updatedAt"], proposal_set_id),
        )

    def _invalidate_alignment_proposal_sets_db(
        self,
        db: sqlite3.Connection,
        project_id: str,
        current_revision: int,
        *,
        exclude_id: str | None = None,
    ) -> None:
        rows = list(
            db.execute(
                "SELECT id,set_json FROM alignment_proposal_sets "
                "WHERE project_id=? AND project_revision<? AND status IN ('PENDING','PARTIALLY_RESOLVED')",
                (project_id, int(current_revision)),
            )
        )
        for row in rows:
            if exclude_id and row["id"] == exclude_id:
                continue
            value = json.loads(row["set_json"])
            value["status"] = "STALE"
            value["invalidationReason"] = "Project revision changed after alignment analysis"
            value["updatedAt"] = now_iso()
            db.execute(
                "UPDATE alignment_proposal_sets SET status='STALE',set_json=?,updated_at=? WHERE id=?",
                (json.dumps(value), value["updatedAt"], row["id"]),
            )

    def _invalidate_alignment_sets_for_library_db(
        self, db: sqlite3.Connection, library_id: str, reason: str
    ) -> None:
        rows = list(
            db.execute(
                "SELECT sets.id,sets.set_json FROM alignment_proposal_sets sets "
                "JOIN projects ON projects.id=sets.project_id "
                "WHERE projects.library_id=? AND sets.status IN ('PENDING','PARTIALLY_RESOLVED')",
                (library_id,),
            )
        )
        for row in rows:
            value = json.loads(row["set_json"])
            value["status"] = "STALE"
            value["invalidationReason"] = reason
            value["updatedAt"] = now_iso()
            db.execute(
                "UPDATE alignment_proposal_sets SET status='STALE',set_json=?,updated_at=? WHERE id=?",
                (json.dumps(value), value["updatedAt"], row["id"]),
            )

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
            db.execute("BEGIN IMMEDIATE")
            if value.get("projectId") is not None:
                project = db.execute(
                    "SELECT revision FROM projects WHERE id=?", (value["projectId"],)
                ).fetchone()
                if not project:
                    raise DomainError("NOT_FOUND", "Project not found")
                if int(project["revision"]) != int(value.get("projectRevision", -1)):
                    value["status"] = "STALE"
                    value["invalidationReason"] = "Project revision changed before suggestion was saved"
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

    def begin_cluster_generation(
        self,
        library_id: str,
        catalog_revision: int,
        event_gap_us: int,
        session_gap_us: int,
    ) -> dict[str, Any]:
        if event_gap_us < 0 or session_gap_us < event_gap_us:
            raise DomainError(
                "VALIDATION_FAILED", "sessionGapUs must be greater than or equal to eventGapUs"
            )
        self.active_library_root_paths(library_id)
        config = {"eventGapUs": int(event_gap_us), "sessionGapUs": int(session_gap_us)}
        config_digest = digest_json(config)
        with self._lock, self.connect() as db:
            library = db.execute(
                "SELECT catalog_revision FROM libraries WHERE id=?", (library_id,)
            ).fetchone()
            if not library:
                raise DomainError("NOT_FOUND", "Library not found")
            if int(library["catalog_revision"]) != int(catalog_revision):
                raise DomainError(
                    "PLAN_STALE", "Catalog changed; refresh the library before clustering"
                )
            active_scan = db.execute(
                "SELECT 1 FROM scan_generations WHERE library_id=? "
                "AND status IN ('QUEUED','RUNNING','CANCEL_REQUESTED')",
                (library_id,),
            ).fetchone()
            if active_scan:
                raise DomainError("JOB_STATE_CONFLICT", "A library scan is still active")
            active_generation = db.execute(
                "SELECT 1 FROM cluster_generations WHERE library_id=? "
                "AND status IN ('QUEUED','RUNNING')",
                (library_id,),
            ).fetchone()
            if active_generation:
                raise DomainError(
                    "JOB_STATE_CONFLICT", "A cluster generation is already active"
                )
            catalog = db.execute(
                "SELECT * FROM catalog_revisions WHERE library_id=? AND revision=?",
                (library_id, int(catalog_revision)),
            ).fetchone()
            if not catalog:
                raise DomainError("PLAN_STALE", "Catalog revision is unavailable")
            generation_id = opaque_id("clusters")
            job_id = opaque_id("job")
            timestamp = now_iso()
            self._create_job_db(
                db, job_id, "CLUSTER_ANALYSIS", library_id=library_id, message="Clustering queued"
            )
            db.execute(
                "INSERT INTO cluster_generations(id,library_id,catalog_revision_id,catalog_revision,job_id,"
                "algorithm,algorithm_version,config_json,config_digest,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation_id,
                    library_id,
                    catalog["id"],
                    int(catalog_revision),
                    job_id,
                    "coverage-gap-hierarchy",
                    "1",
                    json.dumps(config),
                    config_digest,
                    "QUEUED",
                    timestamp,
                    timestamp,
                ),
            )
            job = self.job(job_id, db)
            job["clusterGenerationId"] = generation_id
            return job

    def build_cluster_generation(
        self, generation_id: str, canceled: Any | None = None
    ) -> dict[str, Any]:
        with self.connect() as metadata:
            generation = metadata.execute(
                "SELECT * FROM cluster_generations WHERE id=?", (generation_id,)
            ).fetchone()
            if not generation:
                raise DomainError("NOT_FOUND", "Cluster generation not found")
            generation = dict(generation)
        config = json.loads(generation["config_json"])
        event_gap_us = int(config["eventGapUs"])
        session_gap_us = int(config["sessionGapUs"])
        job_id = str(generation["job_id"])
        library_id = str(generation["library_id"])
        writer = self.connect()
        reader = self.connect()
        try:
            writer.execute("DELETE FROM unclustered_memberships WHERE generation_id=?", (generation_id,))
            writer.execute("DELETE FROM cluster_memberships WHERE generation_id=?", (generation_id,))
            writer.execute("DELETE FROM event_clusters WHERE generation_id=?", (generation_id,))
            writer.execute("DELETE FROM session_clusters WHERE generation_id=?", (generation_id,))
            writer.execute(
                "UPDATE cluster_generations SET status='RUNNING',message=?,updated_at=? WHERE id=?",
                ("Building sessions and events", now_iso(), generation_id),
            )
            self._transition_job_db(
                writer, job_id, "RUNNING", 0.02, "Building sessions and events"
            )
            writer.commit()
            total = int(
                reader.execute(
                    "SELECT COUNT(*) FROM media WHERE library_id=? AND missing=0", (library_id,)
                ).fetchone()[0]
            )
            processed = 0
            unclustered_count = 0
            last_unknown_id = ""
            while True:
                unknown_rows = list(
                    reader.execute(
                        "SELECT id FROM media WHERE library_id=? AND missing=0 "
                        "AND captured_at IS NULL AND id>? ORDER BY id LIMIT 1000",
                        (library_id, last_unknown_id),
                    )
                )
                if not unknown_rows:
                    break
                unknown_batch = [
                    (generation_id, row["id"], json.dumps(["TIMESTAMP_UNRESOLVED"]))
                    for row in unknown_rows
                ]
                writer.executemany(
                    "INSERT INTO unclustered_memberships(generation_id,asset_id,warnings_json) VALUES(?,?,?)",
                    unknown_batch,
                )
                processed += len(unknown_batch)
                unclustered_count += len(unknown_batch)
                last_unknown_id = str(unknown_rows[-1]["id"])
                self._cluster_progress_db(writer, job_id, processed, total, canceled)

            session_state: dict[str, Any] | None = None
            event_state: dict[str, Any] | None = None
            membership_batch: list[tuple[Any, ...]] = []
            session_count = 0
            event_count = 0
            clustered_count = 0

            def finish_event() -> None:
                nonlocal event_state
                if event_state is None:
                    return
                writer.execute(
                    "UPDATE event_clusters SET end_us=?,clip_count=?,source_count=?,root_count=?,warnings_json=? "
                    "WHERE id=?",
                    (
                        event_state["endUs"],
                        event_state["clipCount"],
                        len(event_state["sources"]),
                        len(event_state["roots"]),
                        json.dumps(sorted(event_state["warnings"])),
                        event_state["id"],
                    ),
                )
                event_state = None

            def finish_session() -> None:
                nonlocal session_state
                finish_event()
                if session_state is None:
                    return
                writer.execute(
                    "UPDATE session_clusters SET end_us=?,event_count=?,clip_count=?,source_count=?,"
                    "root_count=?,warnings_json=? WHERE id=?",
                    (
                        session_state["endUs"],
                        session_state["eventCount"],
                        session_state["clipCount"],
                        len(session_state["sources"]),
                        len(session_state["roots"]),
                        json.dumps(sorted(session_state["warnings"])),
                        session_state["id"],
                    ),
                )
                session_state = None

            def start_session(start_us: int, end_us: int) -> None:
                nonlocal session_state, session_count
                session_count += 1
                session_id = _stable_migration_id("session", generation_id, session_count)
                session_state = {
                    "id": session_id,
                    "startUs": start_us,
                    "endUs": end_us,
                    "eventCount": 0,
                    "clipCount": 0,
                    "sources": set(),
                    "roots": set(),
                    "warnings": set(),
                }
                writer.execute(
                    "INSERT INTO session_clusters(id,generation_id,ordinal,start_us,end_us) VALUES(?,?,?,?,?)",
                    (session_id, generation_id, session_count, start_us, end_us),
                )

            def start_event(start_us: int, end_us: int) -> None:
                nonlocal event_state, event_count
                if session_state is None:
                    raise DomainError("INTERNAL_ERROR", "Event cannot exist without a session")
                event_count += 1
                session_state["eventCount"] += 1
                event_id = _stable_migration_id("event", generation_id, event_count)
                event_state = {
                    "id": event_id,
                    "startUs": start_us,
                    "endUs": end_us,
                    "clipCount": 0,
                    "sources": set(),
                    "roots": set(),
                    "warnings": set(),
                }
                writer.execute(
                    "INSERT INTO event_clusters(id,generation_id,session_id,ordinal,session_ordinal,"
                    "start_us,end_us) VALUES(?,?,?,?,?,?,?)",
                    (
                        event_id,
                        generation_id,
                        session_state["id"],
                        event_count,
                        session_state["eventCount"],
                        start_us,
                        end_us,
                    ),
                )

            last_captured_at = ""
            last_timed_id = ""
            while True:
                timed_rows = list(
                    reader.execute(
                        "SELECT id,root_id,captured_at,record_json FROM media "
                        "WHERE library_id=? AND missing=0 AND captured_at IS NOT NULL "
                        "AND (captured_at>? OR (captured_at=? AND id>?)) "
                        "ORDER BY captured_at,id LIMIT 1000",
                        (library_id, last_captured_at, last_captured_at, last_timed_id),
                    )
                )
                if not timed_rows:
                    break
                for row in timed_rows:
                    payload = json.loads(row["record_json"])
                    start_us = _timestamp_to_us(row["captured_at"])
                    if start_us is None:
                        writer.execute(
                            "INSERT INTO unclustered_memberships(generation_id,asset_id,warnings_json) "
                            "VALUES(?,?,?)",
                            (generation_id, row["id"], json.dumps(["TIMESTAMP_UNRESOLVED"])),
                        )
                        processed += 1
                        unclustered_count += 1
                        continue
                    end_us = start_us + max(1, int(payload.get("durationUs") or 0))
                    if session_state is None or start_us > session_state["endUs"] + session_gap_us:
                        finish_session()
                        start_session(start_us, end_us)
                        start_event(start_us, end_us)
                    elif event_state is None or start_us > event_state["endUs"] + event_gap_us:
                        finish_event()
                        start_event(start_us, end_us)
                    if session_state is None or event_state is None:
                        raise DomainError("INTERNAL_ERROR", "Cluster state was not initialized")
                    warnings = _cluster_timing_warnings(payload)
                    root_id = row["root_id"]
                    source_candidate_id = payload.get("sourceCandidateId")
                    session_state["endUs"] = max(session_state["endUs"], end_us)
                    session_state["clipCount"] += 1
                    event_state["endUs"] = max(event_state["endUs"], end_us)
                    event_state["clipCount"] += 1
                    if root_id:
                        session_state["roots"].add(root_id)
                        event_state["roots"].add(root_id)
                    if source_candidate_id:
                        session_state["sources"].add(source_candidate_id)
                        event_state["sources"].add(source_candidate_id)
                    session_state["warnings"].update(warnings)
                    event_state["warnings"].update(warnings)
                    membership_batch.append(
                        (
                            generation_id,
                            session_state["id"],
                            event_state["id"],
                            row["id"],
                            start_us,
                            end_us,
                            root_id,
                            source_candidate_id,
                            json.dumps(warnings),
                        )
                    )
                    clustered_count += 1
                    processed += 1
                if membership_batch:
                    writer.executemany(
                        "INSERT INTO cluster_memberships(generation_id,session_id,event_id,asset_id,start_us,"
                        "end_us,root_id,source_candidate_id,warnings_json) VALUES(?,?,?,?,?,?,?,?,?)",
                        membership_batch,
                    )
                    membership_batch.clear()
                last_captured_at = str(timed_rows[-1]["captured_at"])
                last_timed_id = str(timed_rows[-1]["id"])
                self._cluster_progress_db(writer, job_id, processed, total, canceled)
            finish_session()
            if canceled:
                canceled()
            current_revision = int(
                writer.execute(
                    "SELECT catalog_revision FROM libraries WHERE id=?", (library_id,)
                ).fetchone()[0]
            )
            if current_revision != int(generation["catalog_revision"]):
                raise DomainError("PLAN_STALE", "Catalog changed while clustering")
            writer.execute(
                "UPDATE cluster_generations SET status='SUCCEEDED',session_count=?,event_count=?,"
                "clustered_asset_count=?,unclustered_asset_count=?,message=?,updated_at=? WHERE id=?",
                (
                    session_count,
                    event_count,
                    clustered_count,
                    unclustered_count,
                    "Cluster generation complete",
                    now_iso(),
                    generation_id,
                ),
            )
            self._transition_job_db(
                writer,
                job_id,
                "SUCCEEDED",
                1,
                f"Created {session_count} sessions and {event_count} events",
                result={
                    "clusterGenerationId": generation_id,
                    "sessionCount": session_count,
                    "eventCount": event_count,
                    "clusteredAssetCount": clustered_count,
                    "unclusteredAssetCount": unclustered_count,
                },
            )
            writer.commit()
            return self.cluster_generation(generation_id)
        except Exception:
            writer.rollback()
            raise
        finally:
            reader.close()
            writer.close()

    def _cluster_progress_db(
        self,
        db: sqlite3.Connection,
        job_id: str,
        processed: int,
        total: int,
        canceled: Any | None,
    ) -> None:
        db.commit()
        if canceled:
            canceled()
        self._transition_job_db(
            db,
            job_id,
            "RUNNING",
            min(0.98, 0.05 + 0.9 * (processed / max(1, total))),
            f"Grouped {processed:,} of {total:,} assets",
        )
        db.commit()

    def abort_cluster_generation(self, generation_id: str, status: str, message: str) -> None:
        if status not in {"FAILED", "CANCELED"}:
            raise DomainError("VALIDATION_FAILED", "Invalid cluster terminal state")
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM unclustered_memberships WHERE generation_id=?", (generation_id,))
            db.execute("DELETE FROM cluster_memberships WHERE generation_id=?", (generation_id,))
            db.execute("DELETE FROM event_clusters WHERE generation_id=?", (generation_id,))
            db.execute("DELETE FROM session_clusters WHERE generation_id=?", (generation_id,))
            db.execute(
                "UPDATE cluster_generations SET status=?,message=?,updated_at=? WHERE id=?",
                (status, message, now_iso(), generation_id),
            )

    def cluster_generation(self, generation_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM cluster_generations WHERE id=?", (generation_id,)
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Cluster generation not found")
            return self._public_cluster_generation(row)

    @staticmethod
    def _public_cluster_generation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "libraryId": row["library_id"],
            "catalogRevisionId": row["catalog_revision_id"],
            "catalogRevision": int(row["catalog_revision"]),
            "jobId": row["job_id"],
            "algorithm": row["algorithm"],
            "algorithmVersion": row["algorithm_version"],
            "config": json.loads(row["config_json"]),
            "configDigest": row["config_digest"],
            "status": row["status"],
            "sessionCount": int(row["session_count"]),
            "eventCount": int(row["event_count"]),
            "clusteredAssetCount": int(row["clustered_asset_count"]),
            "unclusteredAssetCount": int(row["unclustered_asset_count"]),
            "message": row["message"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def cluster_generations_page(
        self, library_id: str, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        after_created, after_id = _decode_page_cursor(cursor, "createdAt", "id")
        with self.connect() as db:
            params: list[Any] = [library_id]
            where = "library_id=?"
            if after_created is not None:
                where += " AND (created_at<? OR (created_at=? AND id<?))"
                params.extend([after_created, after_created, after_id])
            rows = list(
                db.execute(
                    f"SELECT * FROM cluster_generations WHERE {where} "
                    "ORDER BY created_at DESC,id DESC LIMIT ?",
                    (*params, limit + 1),
                )
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            _encode_page_cursor(createdAt=rows[-1]["created_at"], id=rows[-1]["id"])
            if has_more and rows
            else None
        )
        return {
            "items": [self._public_cluster_generation(row) for row in rows],
            "nextCursor": next_cursor,
        }

    def cluster_summaries_page(
        self,
        generation_id: str,
        kind: str,
        limit: int = 100,
        cursor: str | None = None,
        session_id: str | None = None,
        root_id: str | None = None,
        source_candidate_id: str | None = None,
        warning_only: bool = False,
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> dict[str, Any]:
        generation = self.cluster_generation(generation_id)
        if generation["status"] != "SUCCEEDED":
            raise DomainError("JOB_STATE_CONFLICT", "Cluster generation is not complete")
        limit = max(1, min(int(limit), 500))
        after_start, after_id = _decode_page_cursor(cursor, "startUs", "id")
        table = "session_clusters" if kind == "SESSION" else "event_clusters"
        where = "generation_id=?"
        params: list[Any] = [generation_id]
        if kind == "EVENT" and session_id is not None:
            where += " AND session_id=?"
            params.append(session_id)
        membership_column = "session_id" if kind == "SESSION" else "event_id"
        if root_id is not None:
            where += (
                f" AND EXISTS(SELECT 1 FROM cluster_memberships members WHERE "
                f"members.{membership_column}={table}.id AND members.root_id=?)"
            )
            params.append(root_id)
        if source_candidate_id is not None:
            where += (
                f" AND EXISTS(SELECT 1 FROM cluster_memberships members WHERE "
                f"members.{membership_column}={table}.id AND members.source_candidate_id=?)"
            )
            params.append(source_candidate_id)
        if warning_only:
            where += " AND warnings_json!='[]'"
        if start_us is not None:
            where += " AND end_us>?"
            params.append(int(start_us))
        if end_us is not None:
            where += " AND start_us<?"
            params.append(int(end_us))
        if after_start is not None:
            where += " AND (start_us>? OR (start_us=? AND id>?))"
            params.extend([int(after_start), int(after_start), after_id])
        with self.connect() as db:
            rows = list(
                db.execute(
                    f"SELECT * FROM {table} WHERE {where} ORDER BY start_us,id LIMIT ?",
                    (*params, limit + 1),
                )
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            items.append(
                {
                    "id": row["id"],
                    "generationId": generation_id,
                    "parentSessionId": row["session_id"] if kind == "EVENT" else None,
                    "kind": kind,
                    "startUs": int(row["start_us"]),
                    "endUs": int(row["end_us"]),
                    "eventCount": int(row["event_count"]) if kind == "SESSION" else 0,
                    "clipCount": int(row["clip_count"]),
                    "sourceCount": int(row["source_count"]),
                    "rootCount": int(row["root_count"]),
                    "warnings": json.loads(row["warnings_json"]),
                }
            )
        next_cursor = (
            _encode_page_cursor(startUs=int(rows[-1]["start_us"]), id=rows[-1]["id"])
            if has_more and rows
            else None
        )
        return {"generationId": generation_id, "items": items, "nextCursor": next_cursor}

    def cluster_facets(self, generation_id: str) -> dict[str, Any]:
        generation = self.cluster_generation(generation_id)
        if generation["status"] != "SUCCEEDED":
            raise DomainError("JOB_STATE_CONFLICT", "Cluster generation is not complete")
        with self.connect() as db:
            roots = [
                {
                    "id": row["root_id"],
                    "label": row["label"],
                    "clipCount": int(row["clip_count"]),
                }
                for row in db.execute(
                    "SELECT members.root_id,roots.label,COUNT(*) AS clip_count "
                    "FROM cluster_memberships members JOIN library_roots roots ON roots.id=members.root_id "
                    "WHERE members.generation_id=? GROUP BY members.root_id,roots.label "
                    "ORDER BY roots.label,members.root_id",
                    (generation_id,),
                )
            ]
            sources = [
                {
                    "id": row["source_candidate_id"],
                    "label": row["label"] or "Unlabelled source candidate",
                    "clipCount": int(row["clip_count"]),
                }
                for row in db.execute(
                    "SELECT members.source_candidate_id,MIN(media.camera) AS label,COUNT(*) AS clip_count "
                    "FROM cluster_memberships members JOIN media ON media.id=members.asset_id "
                    "WHERE members.generation_id=? AND members.source_candidate_id IS NOT NULL "
                    "GROUP BY members.source_candidate_id ORDER BY label,members.source_candidate_id",
                    (generation_id,),
                )
            ]
        return {"generationId": generation_id, "roots": roots, "sourceCandidates": sources}

    def cluster_memberships_page(
        self,
        cluster_id: str,
        kind: str,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        column = "session_id" if kind == "SESSION" else "event_id"
        cluster_table = "session_clusters" if kind == "SESSION" else "event_clusters"
        after_start, after_id = _decode_page_cursor(cursor, "startUs", "assetId")
        where = f"members.{column}=?"
        params: list[Any] = [cluster_id]
        if after_start is not None:
            where += " AND (members.start_us>? OR (members.start_us=? AND members.asset_id>?))"
            params.extend([int(after_start), int(after_start), after_id])
        with self.connect() as db:
            cluster = db.execute(
                f"SELECT clusters.generation_id,generations.status FROM {cluster_table} clusters "
                "JOIN cluster_generations generations ON generations.id=clusters.generation_id "
                "WHERE clusters.id=?",
                (cluster_id,),
            ).fetchone()
            if not cluster:
                raise DomainError("NOT_FOUND", "Cluster not found")
            if cluster["status"] != "SUCCEEDED":
                raise DomainError("JOB_STATE_CONFLICT", "Cluster generation is not complete")
            rows = list(
                db.execute(
                    "SELECT members.*,media.record_json,media.missing FROM cluster_memberships members "
                    f"JOIN media ON media.id=members.asset_id WHERE {where} "
                    "ORDER BY members.start_us,members.asset_id LIMIT ?",
                    (*params, limit + 1),
                )
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            media = json.loads(row["record_json"])
            media["missing"] = bool(row["missing"])
            items.append(
                {
                    "assetId": row["asset_id"],
                    "startUs": int(row["start_us"]),
                    "endUs": int(row["end_us"]),
                    "rootId": row["root_id"],
                    "sourceCandidateId": row["source_candidate_id"],
                    "warnings": json.loads(row["warnings_json"]),
                    "media": media,
                }
            )
        next_cursor = (
            _encode_page_cursor(startUs=int(rows[-1]["start_us"]), assetId=rows[-1]["asset_id"])
            if has_more and rows
            else None
        )
        return {
            "generationId": cluster["generation_id"],
            "clusterId": cluster_id,
            "items": items,
            "nextCursor": next_cursor,
        }

    def unclustered_memberships_page(
        self, generation_id: str, limit: int = 200, cursor: str | None = None
    ) -> dict[str, Any]:
        generation = self.cluster_generation(generation_id)
        if generation["status"] != "SUCCEEDED":
            raise DomainError("JOB_STATE_CONFLICT", "Cluster generation is not complete")
        limit = max(1, min(int(limit), 500))
        after_id, _unused = _decode_page_cursor(cursor, "assetId", "unused")
        with self.connect() as db:
            rows = list(
                db.execute(
                    "SELECT unknown.asset_id,unknown.warnings_json,media.record_json,media.missing "
                    "FROM unclustered_memberships unknown JOIN media ON media.id=unknown.asset_id "
                    "WHERE unknown.generation_id=? AND unknown.asset_id>? ORDER BY unknown.asset_id LIMIT ?",
                    (generation_id, str(after_id or ""), limit + 1),
                )
            )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            media = json.loads(row["record_json"])
            media["missing"] = bool(row["missing"])
            items.append(
                {
                    "assetId": row["asset_id"],
                    "warnings": json.loads(row["warnings_json"]),
                    "media": media,
                }
            )
        next_cursor = (
            _encode_page_cursor(assetId=rows[-1]["asset_id"], unused="")
            if has_more and rows
            else None
        )
        return {"generationId": generation_id, "items": items, "nextCursor": next_cursor}

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
        if row["status"] == "CANCEL_REQUESTED" and status not in {
            "CANCEL_REQUESTED",
            "CANCELED",
            "FAILED",
            "FAILED_RECOVERABLE",
        }:
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

    def register_cache_entry(
        self,
        key: str,
        kind: str,
        path: Path,
        size_bytes: int,
        *,
        pinned: bool = False,
        prune: bool = True,
    ) -> None:
        cache_root = (self.path.parent / "cache").resolve()
        target = path.resolve()
        if not target.is_relative_to(cache_root) or not target.is_file():
            raise DomainError("FORBIDDEN", "Derived cache entry must remain beneath state cache")
        timestamp = now_iso()
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO cache_entries(key,kind,path,size_bytes,pinned,last_accessed,created_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "kind=excluded.kind,path=excluded.path,size_bytes=excluded.size_bytes,pinned=excluded.pinned,"
                "last_accessed=excluded.last_accessed",
                (key, kind, str(target), max(0, int(size_bytes)), int(pinned), timestamp, timestamp),
            )
        if prune:
            self.prune_cache()

    def touch_cache_entry(self, key: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("UPDATE cache_entries SET last_accessed=? WHERE key=?", (now_iso(), key))

    def remove_cache_entry(self, key: str) -> None:
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM cache_entries WHERE key=?", (key,))

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
                elif row["kind"] == "CLUSTER_ANALYSIS":
                    generation = db.execute(
                        "SELECT id FROM cluster_generations WHERE job_id=?", (row["id"],)
                    ).fetchone()
                    if generation:
                        db.execute(
                            "DELETE FROM unclustered_memberships WHERE generation_id=?",
                            (generation["id"],),
                        )
                        db.execute(
                            "DELETE FROM cluster_memberships WHERE generation_id=?",
                            (generation["id"],),
                        )
                        db.execute(
                            "DELETE FROM event_clusters WHERE generation_id=?",
                            (generation["id"],),
                        )
                        db.execute(
                            "DELETE FROM session_clusters WHERE generation_id=?",
                            (generation["id"],),
                        )
                        db.execute(
                            "UPDATE cluster_generations SET status='FAILED',message=?,updated_at=? WHERE id=?",
                            (message, now_iso(), generation["id"]),
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
        acknowledged = sorted(set(warnings))
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan_row = db.execute(
                "SELECT plan_json FROM render_plans WHERE id=?", (plan_id,)
            ).fetchone()
            if not plan_row:
                raise DomainError("NOT_FOUND", "Render plan not found")
            plan = json.loads(plan_row["plan_json"])
            project_row = db.execute(
                "SELECT revision,document_json FROM projects WHERE id=?", (plan["projectId"],)
            ).fetchone()
            if not project_row:
                raise DomainError("NOT_FOUND", "Project not found")
            project = self._migrate_legacy_project(json.loads(project_row["document_json"]))
            current_revision = int(project_row["revision"])
            if int(project["revision"]) != current_revision:
                raise DomainError("INTERNAL_ERROR", "Project revision storage is inconsistent")
            if current_revision != int(plan["projectRevision"]):
                raise DomainError("PLAN_STALE", "Project changed after render plan was created")
            if int(project.get("provenanceRevision", 0)) != int(plan["provenanceRevision"]):
                raise DomainError("PLAN_STALE", "Provenance resolution changed after render plan was created")
            if set(plan.get("warningCodes", [])) - set(acknowledged):
                raise DomainError("VALIDATION_FAILED", "Every render-plan warning must be acknowledged")
            created_at = now_iso()
            attestation = {
                "id": opaque_id("review"),
                "renderPlanId": plan_id,
                "projectId": plan["projectId"],
                "projectRevision": plan["projectRevision"],
                "planDigest": plan["planDigest"],
                "sourceSetDigest": plan["sourceSetDigest"],
                "provenanceRevision": plan["provenanceRevision"],
                "acknowledgedWarnings": acknowledged,
                "createdAt": created_at,
            }
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
            project["updatedAt"] = created_at
            updated = db.execute(
                "UPDATE projects SET document_json=?,updated_at=? WHERE id=? AND revision=?",
                (json.dumps(project), created_at, project["id"], current_revision),
            )
            if updated.rowcount != 1:
                raise DomainError("PLAN_STALE", "Project changed while review was being recorded")
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
            db.execute("BEGIN IMMEDIATE")
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


def _project_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    removed = sorted(set(before) - set(after))
    changed = {
        key: copy.deepcopy(value)
        for key, value in after.items()
        if key not in before or before[key] != value
    }
    return {"set": changed, "remove": removed}


def _apply_project_delta(project: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(project)
    for key in delta.get("remove", []):
        result.pop(str(key), None)
    for key, value in dict(delta.get("set") or {}).items():
        result[str(key)] = copy.deepcopy(value)
    return result


def _delta_command_result(
    before: dict[str, Any],
    after: dict[str, Any],
    full_result: dict[str, Any],
    previous_compiled: dict[str, Any] | None,
    compiled: dict[str, Any],
) -> dict[str, Any]:
    prior_issues = {
        str(item["id"]): item for item in (previous_compiled or compiled).get("issues", [])
    }
    current_issues = {str(item["id"]): item for item in compiled.get("issues", [])}
    return {
        "commandId": full_result["commandId"],
        "projectId": full_result["projectId"],
        "previousRevision": full_result["previousRevision"],
        "appliedRevision": full_result["appliedRevision"],
        "changedEntities": _project_delta(before, after),
        "projectSummary": {
            "id": after["id"],
            "name": after["name"],
            "revision": after["revision"],
            "updatedAt": after["updatedAt"],
            "archived": bool(after.get("archived", False)),
            "preparation": full_result["preparation"],
        },
        "issueDelta": {
            "added": [
                current_issues[item_id]
                for item_id in sorted(set(current_issues) - set(prior_issues))
            ],
            "removedIds": sorted(set(prior_issues) - set(current_issues)),
            "current": list(compiled.get("issues", [])),
        },
        "reviewState": full_result["reviewState"],
        "eventCursor": full_result["eventCursor"],
        "preview": full_result["preview"],
    }


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


def _merge_time_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted((int(start), int(end)) for start, end in ranges if end > start):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _storage_relative_path(root_id: str, relative_path: str) -> str:
    return f"{root_id}::{relative_path}"


def _encode_page_cursor(**values: Any) -> str:
    raw = json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_page_cursor(
    cursor: str | None, first_key: str, second_key: str
) -> tuple[Any | None, Any | None]:
    if cursor is None:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        return value[first_key], value[second_key]
    except (
        binascii.Error,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as error:
        raise DomainError("VALIDATION_FAILED", "Invalid pagination cursor") from error


def _timestamp_to_us(value: Any) -> int | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    delta = parsed.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _cluster_timing_warnings(media: dict[str, Any]) -> list[str]:
    warnings: set[str] = set()
    policy = media.get("custom", {}).get("timestampPolicy", {})
    ambiguity = policy.get("ambiguity")
    if ambiguity not in {"EXPLICIT_OFFSET", "UNAMBIGUOUS_LOCAL_TIME"}:
        warnings.add("TIMING_UNCERTAIN")
    if media.get("warning"):
        warnings.add("MEDIA_WARNING")
    if not media.get("sourceCandidateId"):
        warnings.add("SOURCE_CANDIDATE_UNKNOWN")
    return sorted(warnings)


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
