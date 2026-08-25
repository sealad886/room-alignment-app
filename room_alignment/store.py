from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import MediaRecord, ScanSummary


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS libraries (
  id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE, last_scan TEXT, summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS media (
  id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  relative_path TEXT NOT NULL, captured_at TEXT, camera TEXT, duration REAL, record_json TEXT NOT NULL,
  UNIQUE(library_id, relative_path)
);
CREATE INDEX IF NOT EXISTS media_library_time ON media(library_id, captured_at);
CREATE INDEX IF NOT EXISTS media_library_camera ON media(library_id, camera);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, library_id TEXT NOT NULL REFERENCES libraries(id),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, document_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS render_jobs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), status TEXT NOT NULL,
  output_path TEXT, progress REAL NOT NULL DEFAULT 0, message TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    def save_scan(self, summary: ScanSummary, records: list[MediaRecord]) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO libraries(id,root,last_scan,summary_json) VALUES(?,?,CURRENT_TIMESTAMP,?) "
                "ON CONFLICT(id) DO UPDATE SET root=excluded.root,last_scan=CURRENT_TIMESTAMP,summary_json=excluded.summary_json",
                (summary.library_id, summary.root, json.dumps(summary.to_dict())),
            )
            for record in records:
                db.execute(
                    "INSERT INTO media(id,library_id,relative_path,captured_at,camera,duration,record_json) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET captured_at=excluded.captured_at,camera=excluded.camera,duration=excluded.duration,record_json=excluded.record_json",
                    (record.id, record.library_id, record.relative_path, record.captured_at, record.camera, record.duration, json.dumps(record.to_dict())),
                )

    def libraries(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) | {"summary": json.loads(row["summary_json"])} for row in db.execute("SELECT * FROM libraries ORDER BY last_scan DESC")]

    def media(self, library_id: str, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT record_json FROM media WHERE library_id=? ORDER BY captured_at,relative_path LIMIT ? OFFSET ?", (library_id, limit, offset))
            return [json.loads(row[0]) for row in rows]

    def save_project(self, project: dict[str, Any]) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO projects(id,name,library_id,document_json) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,library_id=excluded.library_id,document_json=excluded.document_json,updated_at=CURRENT_TIMESTAMP",
                (project["id"], project["name"], project["libraryId"], json.dumps(project)),
            )

    def project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT document_json FROM projects WHERE id=?", (project_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def projects(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT document_json FROM projects ORDER BY updated_at DESC")]

    def library_root(self, library_id: str) -> Path:
        with self.connect() as db:
            row = db.execute("SELECT root FROM libraries WHERE id=?", (library_id,)).fetchone()
            if not row:
                raise KeyError("Unknown library")
            return Path(row[0])

    def media_record(self, media_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT record_json FROM media WHERE id=?", (media_id,)).fetchone()
            if not row:
                raise KeyError(f"Unknown media: {media_id}")
            return json.loads(row[0])

    def upsert_job(self, job: dict[str, Any]) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO render_jobs(id,project_id,status,output_path,progress,message) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status,output_path=excluded.output_path,progress=excluded.progress,message=excluded.message,updated_at=CURRENT_TIMESTAMP",
                (job["id"], job["projectId"], job["status"], job.get("outputPath"), job.get("progress", 0), job.get("message")),
            )

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

