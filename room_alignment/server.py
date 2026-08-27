from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import time
import uuid
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from .alignment import (
    AlignmentCanceled,
    AudioSignatureCache,
    analyze_project_alignment,
)
from .domain import DomainError, program_at
from .lifecycle import clear_owner, write_owner
from .render import CanonicalRenderManager, RenderManager, build_render_plan
from .scanner import iter_scan_records
from .store import Store, TERMINAL_JOB_STATES


PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent


def _resource_directory(name: str) -> Path:
    """Return installed package data, with a source-checkout fallback."""

    installed = PACKAGE_ROOT / name
    if installed.is_dir():
        return installed
    return SOURCE_ROOT / name


WEB = _resource_directory("web")
CONTRACTS = _resource_directory("contracts")
CONTRACT = CONTRACTS / "openapi.json"
MAX_BODY = 2_000_000
SESSION_COOKIE = "ra_session"
SESSION_TTL_SECONDS = 43_200


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """Keep connection failures from dumping local paths into diagnostics."""

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exception()
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        print(json.dumps({"component": "http", "error": type(error).__name__ if error else "UnknownError"}))


class SessionManager:
    def __init__(self) -> None:
        self.bootstrap_token = secrets.token_urlsafe(32)
        self.bootstrap_used = False
        self.sessions: dict[str, dict[str, object]] = {}
        self.event_tokens: dict[str, dict[str, object]] = {}
        self.lock = threading.RLock()

    def bootstrap(self, token: str) -> tuple[str, str] | None:
        with self.lock:
            if self.bootstrap_used or not secrets.compare_digest(token, self.bootstrap_token):
                return None
            self.bootstrap_used = True
            session_id = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self.sessions[session_id] = {"csrf": csrf, "created": time.monotonic()}
            self.bootstrap_token = ""
            return session_id, csrf

    def session(self, session_id: str | None) -> dict[str, object] | None:
        if not session_id:
            return None
        with self.lock:
            value = self.sessions.get(session_id)
            if value and time.monotonic() - float(value["created"]) >= SESSION_TTL_SECONDS:
                self.sessions.pop(session_id, None)
                for token, event in list(self.event_tokens.items()):
                    if event.get("sessionId") == session_id:
                        self.event_tokens.pop(token, None)
                return None
            return value

    def event_token(self, session_id: str) -> dict[str, object]:
        with self.lock:
            current = time.monotonic()
            for existing_token, value in list(self.event_tokens.items()):
                if float(value["expiresAt"]) < current:
                    self.event_tokens.pop(existing_token, None)
            token = secrets.token_urlsafe(32)
            expires_at = current + 60
            self.event_tokens[token] = {"sessionId": session_id, "expiresAt": expires_at}
            return {"token": token, "expiresInSeconds": 60}

    def validate_event_token(self, token: str, session_id: str) -> bool:
        with self.lock:
            value = self.event_tokens.get(token)
            if not value:
                return False
            if value["expiresAt"] < time.monotonic():
                self.event_tokens.pop(token, None)
                return False
            return secrets.compare_digest(str(value["sessionId"]), session_id)


class App:
    def __init__(self, data_dir: Path):
        """Initialize application state in the specified data directory.
        
        Parameters:
        	data_dir (Path): Directory for persistent application data and the process ownership lock.
        
        Raises:
        	RuntimeError: If another Room Alignment process already owns the data directory.
        """
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.data_dir / "application.lock").open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock_file.close()
            raise RuntimeError("Another Room Alignment process owns this state directory") from error
        write_owner(self._lock_file)
        self.store = Store(self.data_dir / "room-alignment.sqlite3")
        self.audio_signatures = AudioSignatureCache(self.store)
        self.legacy_render = RenderManager(self.store)
        self.render = CanonicalRenderManager(self.store)
        self.sessions = SessionManager()
        self.scan_threads: dict[str, threading.Thread] = {}
        self.analysis_threads: dict[str, threading.Thread] = {}
        self.analysis_reserved: set[str] = set()
        self.alignment_projects_reserved: set[str] = set()
        self.hash_slot = threading.BoundedSemaphore(1)
        self.lock = threading.RLock()
        self.closing = False

    def close(self) -> None:
        """Shut down the application and release its resources.
        
        Requests active work to stop, shuts down rendering, waits up to five seconds
        for worker threads to settle, marks remaining jobs as interrupted, and releases
        the application lock.
        """
        self.closing = True
        with self.lock:
            scans = dict(self.scan_threads)
            analyses = dict(self.analysis_threads)
        for scan_id in scans:
            try:
                self.store.cancel_scan(scan_id)
            except DomainError:
                pass
        for job_id in analyses:
            try:
                job = self.store.job(job_id)
                if job["status"] not in TERMINAL_JOB_STATES:
                    self.store.transition_job(job_id, "CANCEL_REQUESTED", job["progress"], "Application is shutting down")
            except DomainError:
                pass
        self.render.shutdown()
        deadline = time.monotonic() + 5
        workers = {**scans, **analyses}
        for thread in workers.values():
            thread.join(timeout=max(0, deadline - time.monotonic()))
        for job_id, thread in workers.items():
            if not thread.is_alive():
                continue
            try:
                job = self.store.job(job_id)
                if job["status"] not in TERMINAL_JOB_STATES:
                    self.store.transition_job(
                        job_id,
                        "INTERRUPTED",
                        job["progress"],
                        "Worker did not settle before the shutdown deadline",
                    )
            except DomainError:
                pass
        try:
            clear_owner(self._lock_file)
        finally:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_file.close()

    def start_scan(
        self,
        library_id: str,
        mode: str,
        limit: int | None = None,
        root_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """
        Start a library scan across the selected roots and track its progress and outcome.
        
        Parameters:
            library_id (str): Identifier of the library to scan.
            mode (str): Scan mode, such as incremental or full traversal.
            limit (int | None): Maximum number of media records to process.
            root_ids (list[str] | None): Root identifiers to scan, or all active roots when omitted.
        
        Returns:
            dict[str, object]: The newly created scan record.
        """
        scan = self.store.begin_scan(library_id, mode, limit, root_ids)
        roots = self.store.active_library_root_paths(library_id, scan["rootIds"])

        def run() -> None:
            cameras: set[str] = set()
            date_groups: dict[str, int] = {}
            warnings = 0
            completed_root_ids: list[str] = []
            failed_roots: list[dict[str, str]] = []
            skipped_root_ids: list[str] = []
            processed = 0
            try:
                for root_index, (root_id, root) in enumerate(roots):
                    if self.closing or self.store.scan_cancel_requested(scan["id"]):
                        break
                    remaining = None if limit is None else max(0, limit - processed)
                    if remaining == 0:
                        for pending_root_id, _pending_root in roots[root_index:]:
                            self.store.finish_scan_root(
                                scan["id"],
                                pending_root_id,
                                "SKIPPED",
                                message="Bound reached before this root",
                            )
                            skipped_root_ids.append(pending_root_id)
                        break
                    batch = []
                    last_progress_at = time.monotonic()
                    try:
                        for record in iter_scan_records(
                            root,
                            library_id,
                            root_id=root_id,
                            mode=mode,
                            existing_batch_lookup=lambda paths, selected=root_id: self.store.existing_media_by_paths(
                                library_id, selected, paths
                            ),
                            canceled=lambda: self.closing or self.store.scan_cancel_requested(scan["id"]),
                            max_files=remaining,
                        ):
                            if self.closing or self.store.scan_cancel_requested(scan["id"]):
                                break
                            if record.camera:
                                cameras.add(record.camera)
                            if record.captured_at:
                                date = str(record.captured_at)[:10]
                                date_groups[date] = date_groups.get(date, 0) + 1
                            warnings += int(bool(record.warning))
                            batch.append(record)
                            if len(batch) >= 50 or time.monotonic() - last_progress_at >= 1:
                                batch_warnings = sum(int(bool(item.warning)) for item in batch)
                                self.store.save_media_batch(scan["id"], batch)
                                self.store.scan_progress(
                                    scan["id"],
                                    root_id=root_id,
                                    processed=len(batch),
                                    warning_count=batch_warnings,
                                    message=f"Indexing folder {root_index + 1} of {len(roots)}",
                                )
                                processed += len(batch)
                                batch.clear()
                                last_progress_at = time.monotonic()
                        if batch:
                            batch_warnings = sum(int(bool(item.warning)) for item in batch)
                            self.store.save_media_batch(scan["id"], batch)
                            self.store.scan_progress(
                                scan["id"],
                                root_id=root_id,
                                processed=len(batch),
                                warning_count=batch_warnings,
                                message=f"Indexing folder {root_index + 1} of {len(roots)}",
                            )
                            processed += len(batch)
                        if self.closing or self.store.scan_cancel_requested(scan["id"]):
                            self.store.finish_scan_root(
                                scan["id"], root_id, "CANCELED", message="Scan interrupted"
                            )
                            break
                        self.store.finish_scan_root(
                            scan["id"],
                            root_id,
                            "SUCCEEDED",
                            full_traversal_completed=mode == "FULL",
                            message="Folder scan complete",
                        )
                        completed_root_ids.append(root_id)
                    except Exception as root_error:
                        failed_roots.append(
                            {
                                "rootId": root_id,
                                "code": getattr(root_error, "code", "INTERNAL_ERROR"),
                                "message": _safe_error(root_error),
                            }
                        )
                        self.store.finish_scan_root(
                            scan["id"], root_id, "FAILED", message=_safe_error(root_error)
                        )
                if self.closing or self.store.scan_cancel_requested(scan["id"]):
                    job = self.store.job(scan["id"])
                    grant_revoked = job.get("errorCode") == "GRANT_REQUIRED"
                    self.store.finish_scan(
                        scan["id"],
                        "FAILED" if grant_revoked else "CANCELED",
                        {
                            "videos": self.store.scan(scan["id"])["videos"],
                            "completedRootIds": completed_root_ids,
                            "failedRoots": failed_roots,
                            "skippedRootIds": skipped_root_ids,
                        },
                        (
                            "Directory grant revoked; visited records retained"
                            if grant_revoked
                            else "Scan canceled; visited records retained"
                        ),
                        "GRANT_REQUIRED" if grant_revoked else None,
                    )
                    return
                current = self.store.scan(scan["id"])
                summary = {
                    "libraryId": library_id,
                    "mode": mode,
                    "scanned": current["scanned"],
                    "videos": current["videos"],
                    "warnings": warnings,
                    "sources": len(cameras),
                    "cameras": sorted(cameras),
                    "dateGroups": date_groups,
                    "completedRootIds": completed_root_ids,
                    "failedRoots": failed_roots,
                    "skippedRootIds": skipped_root_ids,
                    "partial": bool(failed_roots),
                }
                if completed_root_ids:
                    message = "Scan complete with folder warnings" if failed_roots else "Scan complete"
                    self.store.finish_scan(scan["id"], "SUCCEEDED", summary, message)
                else:
                    self.store.finish_scan(
                        scan["id"],
                        "FAILED",
                        summary,
                        "No selected folder could be scanned",
                        failed_roots[0]["code"] if failed_roots else "INTERNAL_ERROR",
                    )
            except Exception as error:
                try:
                    self.store.finish_scan(
                        scan["id"],
                        "FAILED",
                        {"videos": self.store.scan(scan["id"])["videos"]},
                        _safe_error(error),
                        getattr(error, "code", "INTERNAL_ERROR"),
                    )
                except Exception:
                    pass
            finally:
                with self.lock:
                    self.scan_threads.pop(scan["id"], None)

        thread = threading.Thread(target=run, daemon=True, name=f"scan-{scan['id']}")
        with self.lock:
            self.scan_threads[scan["id"]] = thread
        thread.start()
        return scan

    def start_alignment_analysis(self, project_id: str) -> dict[str, object]:
        """
        Start a cancellable background alignment analysis for a project.
        
        Parameters:
            project_id (str): Identifier of the project to analyze.
        
        Returns:
            dict[str, object]: The queued analysis job.
        
        Raises:
            DomainError: If two analysis jobs are already running or the project
                already has an active alignment analysis.
        """
        project = self.store.project(project_id)
        settings = self.store.application_settings()
        self.store.active_library_root_paths(project["libraryId"])
        with self.lock:
            if len(self.analysis_reserved) >= 2:
                raise DomainError("JOB_STATE_CONFLICT", "At most two analysis jobs may run concurrently")
            if project_id in self.alignment_projects_reserved:
                raise DomainError("JOB_STATE_CONFLICT", "Alignment analysis already runs for this project")
            job = self.store.create_job("ALIGNMENT_ANALYSIS", project_id=project_id, message="Analysis queued")
            self.analysis_reserved.add(job["id"])
            self.alignment_projects_reserved.add(project_id)

        def run() -> None:
            """
            Run the alignment analysis job and record its proposal set or failure state.
            
            The analysis responds to cancellation requests, reports progress, saves the resulting
            non-mutating proposal set, and releases the job's analysis reservations when complete.
            """
            try:
                self._raise_if_job_stopping(job["id"])
                self.store.transition_job(job["id"], "RUNNING", 0.05, "Preparing bounded overlap candidates")
                assets = self.store.media_records(item["assetId"] for item in project["clips"])
                proposal_set = analyze_project_alignment(
                    project,
                    assets,
                    self.audio_signatures,
                    overlap_search_extension_us=int(settings["overlapSearchExtensionUs"]),
                    canceled=lambda: self.closing or self._job_stopping(job["id"]),
                    progress=lambda value, message: self.store.transition_job(
                        job["id"], "RUNNING", value, message
                    ),
                )
                proposal_set = self.store.save_alignment_proposal_set(proposal_set)
                self._raise_if_job_stopping(job["id"])
                self.store.transition_job(
                    job["id"],
                    "SUCCEEDED",
                    1,
                    "Created one non-mutating alignment proposal set",
                    result={
                        "proposalSetId": proposal_set["id"],
                        "proposalSetDigest": proposal_set["digest"],
                        "summary": proposal_set["summary"],
                    },
                )
            except AlignmentCanceled as error:
                self._finish_analysis_error(
                    job["id"], DomainError("JOB_STATE_CONFLICT", str(error))
                )
            except Exception as error:
                self._finish_analysis_error(job["id"], error)
            finally:
                with self.lock:
                    self.analysis_threads.pop(job["id"], None)
                    self.analysis_reserved.discard(job["id"])
                    self.alignment_projects_reserved.discard(project_id)

        thread = threading.Thread(target=run, daemon=True, name=f"analysis-{job['id']}")
        with self.lock:
            self.analysis_threads[job["id"]] = thread
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.analysis_threads.pop(job["id"], None)
                self.analysis_reserved.discard(job["id"])
                self.alignment_projects_reserved.discard(project_id)
            self.store.transition_job(job["id"], "FAILED", 0, "Analysis worker could not start")
            raise
        return job

    def start_cluster_analysis(
        self,
        library_id: str,
        catalog_revision: int | None = None,
        event_gap_us: int | None = None,
        session_gap_us: int | None = None,
    ) -> dict[str, object]:
        library = self.store.library(library_id)
        catalog_revision = int(
            library["catalogRevision"] if catalog_revision is None else catalog_revision
        )
        event_gap_us = int(library["eventGapUs"] if event_gap_us is None else event_gap_us)
        session_gap_us = int(
            library["sessionGapUs"] if session_gap_us is None else session_gap_us
        )
        with self.lock:
            if len(self.analysis_reserved) >= 2:
                raise DomainError("JOB_STATE_CONFLICT", "At most two analysis jobs may run concurrently")
            job = self.store.begin_cluster_generation(
                library_id,
                catalog_revision,
                event_gap_us,
                session_gap_us,
            )
            self.analysis_reserved.add(job["id"])
        generation_id = str(job["clusterGenerationId"])

        def run() -> None:
            try:
                self._raise_if_job_stopping(job["id"])
                self.store.build_cluster_generation(
                    generation_id,
                    canceled=lambda: self._raise_if_job_stopping(job["id"]),
                )
            except Exception as error:
                try:
                    state = self.store.job(job["id"])
                    terminal = (
                        "CANCELED"
                        if state["status"] == "CANCEL_REQUESTED"
                        and state.get("errorCode") != "GRANT_REQUIRED"
                        else "FAILED"
                    )
                    self.store.abort_cluster_generation(generation_id, terminal, _safe_error(error))
                except Exception:
                    pass
                self._finish_analysis_error(job["id"], error)
            finally:
                with self.lock:
                    self.analysis_threads.pop(job["id"], None)
                    self.analysis_reserved.discard(job["id"])

        thread = threading.Thread(target=run, daemon=True, name=f"cluster-{job['id']}")
        with self.lock:
            self.analysis_threads[job["id"]] = thread
        try:
            thread.start()
        except Exception:
            with self.lock:
                self.analysis_threads.pop(job["id"], None)
                self.analysis_reserved.discard(job["id"])
            self.store.abort_cluster_generation(
                generation_id, "FAILED", "Analysis worker could not start"
            )
            self.store.transition_job(job["id"], "FAILED", 0, "Analysis worker could not start")
            raise
        return job

    def _raise_if_job_stopping(self, job_id: str) -> None:
        job = self.store.job(job_id)
        if job["status"] == "CANCEL_REQUESTED":
            code = str(job.get("errorCode") or "JOB_STATE_CONFLICT")
            message = "Directory grant was revoked" if code == "GRANT_REQUIRED" else "Job was canceled"
            raise DomainError(code, message)

    def _job_stopping(self, job_id: str) -> bool:
        return self.store.job(job_id)["status"] == "CANCEL_REQUESTED"

    def _finish_analysis_error(self, job_id: str, error: Exception) -> None:
        self.store.finish_job_error(
            job_id,
            _safe_error(error),
            getattr(error, "code", "INTERNAL_ERROR"),
        )


APP: App


class Handler(BaseHTTPRequestHandler):
    server_version = "RoomAlignment/0.2"

    def log_message(self, format: str, *args: object) -> None:
        path = urlparse(self.path).path
        route = "/bootstrap/[redacted]" if path.startswith("/bootstrap/") else path
        print(
            json.dumps(
                {
                    "component": "http",
                    "method": self.command,
                    "route": route,
                    # BaseHTTPRequestHandler's default message contains the
                    # complete request target. Never duplicate bootstrap or
                    # event credentials into diagnostics.
                    "status": str(args[1]) if len(args) > 1 else None,
                }
            )
        )

    def request_id(self) -> str:
        return getattr(self, "_request_id", "") or self._set_request_id()

    def _set_request_id(self) -> str:
        self._request_id = f"req_{uuid.uuid4().hex}"
        return self._request_id

    def json_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise DomainError("VALIDATION_FAILED", "Invalid Content-Length") from error
        if length < 0 or length > MAX_BODY:
            raise DomainError("VALIDATION_FAILED", "Request body exceeds 2 MB")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            raise DomainError("VALIDATION_FAILED", "Request body is not valid JSON") from error
        if not isinstance(payload, dict):
            raise DomainError("VALIDATION_FAILED", "Request body must be a JSON object")
        return payload

    def session_id(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        value = cookie.get(SESSION_COOKIE)
        return value.value if value else None

    def session(self) -> tuple[str, dict[str, object]]:
        session_id = self.session_id()
        session = APP.sessions.session(session_id)
        if not session_id or not session:
            raise DomainError("UNAUTHENTICATED", "Open the application from its secure launch URL")
        return session_id, session

    def enforce_request_boundary(self, mutation: bool = False) -> tuple[str, dict[str, object]] | None:
        if not _trusted_host(self.headers.get("Host", ""), self.server.server_port):
            raise DomainError("FORBIDDEN", "Local Host header required")
        if not self.path.startswith("/api/v1/"):
            return None
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site not in {None, "same-origin", "none"}:
            raise DomainError("FORBIDDEN", "Cross-site API access denied")
        origin = self.headers.get("Origin")
        if origin and not _trusted_origin(origin, self.server.server_port):
            raise DomainError("FORBIDDEN", "Same-origin API access required")
        session_id, session = self.session()
        if mutation:
            csrf = self.headers.get("X-CSRF-Token", "")
            if not csrf or not secrets.compare_digest(csrf, str(session["csrf"])):
                raise DomainError("FORBIDDEN", "Valid CSRF token required")
        return session_id, session

    def respond(self, payload: object, status: int = 200, content_type: str = "application/json; charset=utf-8") -> None:
        if isinstance(payload, bytes):
            body = payload
        elif content_type.startswith(("application/json", "application/schema+json")):
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self.request_id())
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self._security_headers()
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def error(self, error: Exception, status: int | None = None) -> None:
        code = getattr(error, "code", "INTERNAL_ERROR")
        details = getattr(error, "details", {})
        if status is None:
            status = {
                "UNAUTHENTICATED": 401,
                "FORBIDDEN": 403,
                "NOT_FOUND": 404,
                "REVISION_CONFLICT": 409,
                "IDEMPOTENCY_CONFLICT": 409,
                "JOB_STATE_CONFLICT": 409,
                "DESTINATION_EXISTS": 409,
                "SOURCE_CHANGED": 409,
                "PLAN_STALE": 409,
                "REVIEW_STALE": 409,
                "GRANT_REQUIRED": 403,
                "INSUFFICIENT_SPACE": 422,
                "COVERAGE_INVALID": 422,
                "UNSUPPORTED_MEDIA": 422,
                "VALIDATION_FAILED": 400,
            }.get(code, 500)
        message = str(error) if code != "INTERNAL_ERROR" else "Internal application error"
        self.respond(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "requestId": self.request_id(),
                    "retryable": code in {"JOB_STATE_CONFLICT"},
                    "details": details,
                }
            },
            status,
        )

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def do_GET(self) -> None:
        self._set_request_id()
        try:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if not _trusted_host(self.headers.get("Host", ""), self.server.server_port):
                raise DomainError("FORBIDDEN", "Local Host header required")
            if path == "/api/health":
                return self.respond({"ok": True, "version": __version__})
            if path.startswith("/bootstrap/"):
                token = unquote(path.removeprefix("/bootstrap/"))
                bootstrapped = APP.sessions.bootstrap(token)
                if not bootstrapped:
                    raise DomainError("UNAUTHENTICATED", "Bootstrap link is invalid or already used")
                session_id, _csrf = bootstrapped
                cookie = f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL_SECONDS}"
                return self.redirect("/", cookie)
            if path == "/api/v1/events":
                session_id, _session = self.session()
                token = query.get("token", [""])[0]
                if not APP.sessions.validate_event_token(token, session_id):
                    raise DomainError("UNAUTHENTICATED", "Event token is invalid or expired")
                return self.stream_events(query)
            if path.startswith("/api/v1/"):
                self.enforce_request_boundary()
                return self.get_api(path, query)
            return self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.error(error)

    def do_HEAD(self) -> None:
        self._set_request_id()
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if not _trusted_host(self.headers.get("Host", ""), self.server.server_port):
                raise DomainError("FORBIDDEN", "Local Host header required")
            self.enforce_request_boundary()
            parts = [part for part in path.split("/") if part]
            if len(parts) == 5 and parts[2] == "media" and parts[4] == "preview":
                return self.stream_source_preview(APP.store.media_source_path(parts[3]))
            raise DomainError("NOT_FOUND", "API resource not found")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.error(error)

    def get_api(self, path: str, query: dict[str, list[str]]) -> None:
        """
        Handle versioned API GET requests and return the requested resource or artifact.
        
        Parameters:
            path (str): API request path.
            query (dict[str, list[str]]): Parsed query parameters used for filtering, pagination, and resource options.
        """
        if path == "/api/v1/system":
            return self.respond(
                {
                    "version": __version__,
                    "apiVersion": "v1",
                    "timeUnit": "microseconds",
                    "intervals": "half-open",
                    "capabilities": {
                        "manualAlignment": True,
                        "alignmentSuggestions": True,
                        "independentAudio": True,
                        "sse": True,
                        "compatibleRender": True,
                        "archivalLosslessRender": True,
                    },
                }
            )
        if path == "/api/v1/session":
            _session_id, session = self.session()
            return self.respond({"authenticated": True, "csrfToken": session["csrf"]})
        if path == "/api/v1/settings":
            return self.respond(APP.store.application_settings())
        if path == "/api/v1/openapi.json":
            return self.respond(CONTRACT.read_bytes(), content_type="application/json; charset=utf-8")
        if path == "/api/v1/grants":
            return self.respond(APP.store.grants())
        if path == "/api/v1/libraries":
            return self.respond(APP.store.libraries())
        if path == "/api/v1/projects":
            return self.respond(APP.store.projects())
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[:3] == ["api", "v1", "contracts"]:
            name = parts[3]
            if name not in {
                "api.schema.json",
                "domain.schema.json",
                "commands.schema.json",
                "manifest.schema.json",
                "timeline.schema.json",
            }:
                raise DomainError("NOT_FOUND", "Contract not found")
            return self.respond((CONTRACTS / name).read_bytes(), content_type="application/schema+json; charset=utf-8")
        if len(parts) == 3 and parts[:2] == ["api", "v1"] and parts[2] == "events":
            return self.respond([])
        if len(parts) == 4 and parts[2] == "scans":
            return self.respond(APP.store.scan(parts[3]))
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "media":
            cursor = query.get("cursor", [None])[0]
            generation = query.get("generation", [None])[0]
            return self.respond(
                APP.store.media_page(
                    parts[3],
                    _int_input(query.get("limit", ["200"])[0], "limit"),
                    cursor,
                    _int_input(generation, "generation") if generation is not None else None,
                )
            )
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "roots":
            return self.respond(APP.store.library_roots(parts[3]))
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "cluster-generations":
            return self.respond(
                APP.store.cluster_generations_page(
                    parts[3],
                    _int_input(query.get("limit", ["50"])[0], "limit"),
                    query.get("cursor", [None])[0],
                )
            )
        if len(parts) == 4 and parts[2] == "cluster-generations":
            return self.respond(APP.store.cluster_generation(parts[3]))
        if (
            len(parts) == 5
            and parts[2] == "cluster-generations"
            and parts[4] in {"sessions", "events"}
        ):
            kind = "SESSION" if parts[4] == "sessions" else "EVENT"
            return self.respond(
                APP.store.cluster_summaries_page(
                    parts[3],
                    kind,
                    _int_input(query.get("limit", ["100"])[0], "limit"),
                    query.get("cursor", [None])[0],
                    query.get("sessionId", [None])[0],
                    query.get("rootId", [None])[0],
                    query.get("sourceCandidateId", [None])[0],
                    query.get("warning", ["false"])[0].lower() == "true",
                    _int_input(query["startUs"][0], "startUs")
                    if query.get("startUs")
                    else None,
                    _int_input(query["endUs"][0], "endUs")
                    if query.get("endUs")
                    else None,
                )
            )
        if (
            len(parts) == 5
            and parts[2] == "cluster-generations"
            and parts[4] == "facets"
        ):
            return self.respond(APP.store.cluster_facets(parts[3]))
        if (
            len(parts) == 5
            and parts[2] == "cluster-generations"
            and parts[4] == "unclustered"
        ):
            return self.respond(
                APP.store.unclustered_memberships_page(
                    parts[3],
                    _int_input(query.get("limit", ["200"])[0], "limit"),
                    query.get("cursor", [None])[0],
                )
            )
        if (
            len(parts) == 5
            and parts[2] in {"session-clusters", "event-clusters"}
            and parts[4] == "memberships"
        ):
            return self.respond(
                APP.store.cluster_memberships_page(
                    parts[3],
                    "SESSION" if parts[2] == "session-clusters" else "EVENT",
                    _int_input(query.get("limit", ["200"])[0], "limit"),
                    query.get("cursor", [None])[0],
                )
            )
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "cluster-suggestions":
            return self.respond(APP.store.library_suggestions(parts[3]))
        if len(parts) == 4 and parts[2] == "media":
            media = APP.store.media_record(parts[3])
            media["resolutions"] = APP.store.provenance_resolutions(parts[3])
            return self.respond(media)
        if len(parts) == 5 and parts[2] == "media" and parts[4] == "preview":
            return self.stream_source_preview(APP.store.media_source_path(parts[3]))
        if len(parts) == 5 and parts[2] == "media" and parts[4] == "waveform":
            media = APP.store.media_record(parts[3])
            return self.respond(
                APP.audio_signatures.cached_waveform(
                    media,
                    _int_input(query.get("startSourceUs", ["0"])[0], "startSourceUs"),
                    _int_input(query["endSourceUs"][0], "endSourceUs")
                    if query.get("endSourceUs")
                    else None,
                    _int_input(query.get("maxPoints", ["240"])[0], "maxPoints"),
                )
            )
        if len(parts) == 6 and parts[2] == "media" and parts[4:] == ["provenance", "resolutions"]:
            return self.respond(APP.store.provenance_resolutions(parts[3], query.get("field", [None])[0]))
        if len(parts) == 4 and parts[2] == "projects":
            return self.respond(APP.store.project(parts[3]))
        if len(parts) == 6 and parts[2] == "projects" and parts[4] == "revisions":
            return self.respond(APP.store.project_revision(parts[3], _int_input(parts[5], "revision")))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "program":
            return self.respond(APP.store.compiled_project(parts[3]))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "alignment-summary":
            return self.respond(APP.store.project_alignment_summary(parts[3]))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "preparation":
            return self.respond(APP.store.project_preparation(parts[3]))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "timeline-section-proposal":
            return self.respond(
                APP.store.project_timeline_section_proposal(
                    parts[3], query.get("gapMode", ["EXCLUDE"])[0]
                )
            )
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "timeline-window":
            lanes = {
                item
                for item in query.get("lane", [""])[0].split(",")
                if item
            }
            return self.respond(
                APP.store.project_timeline_window(
                    parts[3],
                    _int_input(_required_query(query, "startAlignedUs"), "startAlignedUs"),
                    _int_input(_required_query(query, "endAlignedUs"), "endAlignedUs"),
                    _int_input(_required_query(query, "resolutionUs"), "resolutionUs"),
                    lanes or None,
                )
            )
        if (
            len(parts) == 5
            and parts[2] == "projects"
            and parts[4] == "alignment-proposal-sets"
        ):
            return self.respond(APP.store.alignment_proposal_sets(parts[3]))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "program-at":
            output_us = _int_input(query.get("outputUs", ["0"])[0], "outputUs")
            compiled = APP.store.compiled_project(parts[3])
            return self.respond(program_at(compiled, output_us))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "suggestions":
            return self.respond(APP.store.suggestions(parts[3]))
        if len(parts) == 4 and parts[2] == "render-plans":
            return self.respond(APP.store.render_plan(parts[3]))
        if len(parts) == 4 and parts[2] == "jobs":
            return self.respond(APP.store.job(parts[3]))
        if len(parts) == 4 and parts[2] == "artifacts":
            return self.respond(APP.store.artifact(parts[3]))
        if len(parts) == 5 and parts[2] == "artifacts" and parts[4] == "manifest":
            artifact = APP.store.artifact(parts[3])
            if artifact["status"] != "COMPLETE":
                raise DomainError("JOB_STATE_CONFLICT", "Artifact manifest is not complete")
            path = APP.store.output_path(artifact["outputGrantId"], artifact["manifestFilename"])
            return self.respond(path.read_bytes(), content_type="application/json; charset=utf-8")
        if len(parts) == 5 and parts[2] == "artifacts" and parts[4] == "video":
            artifact = APP.store.artifact(parts[3])
            if artifact["status"] != "COMPLETE":
                raise DomainError("JOB_STATE_CONFLICT", "Artifact video is not complete")
            path = APP.store.output_path(artifact["outputGrantId"], artifact["filename"])
            return self.stream_file(path)
        raise DomainError("NOT_FOUND", "API resource not found")

    def stream_file(self, path: Path) -> None:
        if any(ord(character) < 32 or ord(character) == 127 for character in path.name):
            raise DomainError("VALIDATION_FAILED", "Artifact filename contains control characters")
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        encoded_name = quote(path.name, safe="")
        fallback_name = "".join(
            character if character.isascii() and character not in {'"', "\\"} else "_"
            for character in path.name
        )
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{fallback_name}\"; filename*=UTF-8''{encoded_name}",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self.request_id())
        self.end_headers()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def stream_source_preview(self, path: Path) -> None:
        size = path.stat().st_size
        start, end = 0, max(0, size - 1)
        range_header = self.headers.get("Range")
        status = HTTPStatus.OK
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or not any(match.groups()):
                raise DomainError("VALIDATION_FAILED", "Only one bounded byte range is supported")
            first, last = match.groups()
            if first:
                start = int(first)
                end = int(last) if last else end
            else:
                suffix = int(last)
                if suffix <= 0:
                    raise DomainError("VALIDATION_FAILED", "Byte-range suffix must be positive")
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self._security_headers()
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Request-ID", self.request_id())
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1 if size else 0
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Request-ID", self.request_id())
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self) -> None:
        """Handle authenticated POST requests and convert failures into HTTP error responses."""
        self._set_request_id()
        try:
            self.enforce_request_boundary(mutation=True)
            path = urlparse(self.path).path
            query = parse_qs(urlparse(self.path).query)
            body = self.json_body()
            return self.post_api(path, query, body)
        except FileNotFoundError:
            self.error(DomainError("VALIDATION_FAILED", "Directory does not exist"))
        except Exception as error:
            self.error(error)

    def do_PUT(self) -> None:
        self._set_request_id()
        try:
            self.enforce_request_boundary(mutation=True)
            path = urlparse(self.path).path
            body = self.json_body()
            if path != "/api/v1/settings":
                raise DomainError("NOT_FOUND", "API resource not found")
            return self.respond(APP.store.update_application_settings(body))
        except Exception as error:
            self.error(error)

    def post_api(self, path: str, query: dict[str, list[str]], body: dict[str, object]) -> None:
        """
        Handle authenticated POST requests for API resources and actions.
        
        Parameters:
            path (str): API route to dispatch.
            query (dict[str, list[str]]): Query parameters controlling request behavior.
            body (dict[str, object]): Parsed JSON request body.
        
        Raises:
            DomainError: If request data is invalid or the API resource is not found.
        """
        if path == "/api/v1/grants":
            return self.respond(
                APP.store.create_grant(
                    Path(str(_required(body, "path"))), str(_required(body, "role"))
                ),
                HTTPStatus.CREATED,
            )
        if path == "/api/v1/libraries":
            source_grant_id = body.get("sourceGrantId")
            if source_grant_id is not None:
                library = APP.store.create_library(
                    str(source_grant_id),
                    str(body.get("timeZone", "UTC")),
                    _int_input(body.get("dstFold", 0), "dstFold"),
                    str(body.get("nonexistentPolicy", "REJECT")),
                )
            else:
                library = APP.store.create_empty_library(
                    str(_required(body, "name")),
                    str(body.get("timeZone", "UTC")),
                    _int_input(body.get("dstFold", 0), "dstFold"),
                    str(body.get("nonexistentPolicy", "REJECT")),
                    _int_input(body.get("eventGapUs", 15_000_000), "eventGapUs"),
                    _int_input(body.get("sessionGapUs", 120_000_000), "sessionGapUs"),
                )
            return self.respond(library, HTTPStatus.CREATED)
        if path == "/api/v1/projects":
            cluster_generation_id = body.get("clusterGenerationId")
            if cluster_generation_id is not None:
                return self.respond(
                    APP.store.create_project_from_selection(
                        str(body.get("name", "Untitled alignment")),
                        str(_required(body, "libraryId")),
                        str(cluster_generation_id),
                        [
                            str(item)
                            for item in _list_input(body.get("sessionIds", []), "sessionIds")
                        ],
                        [
                            str(item)
                            for item in _list_input(body.get("eventIds", []), "eventIds")
                        ],
                        [
                            str(item)
                            for item in _list_input(
                                body.get("includeAssetIds", []), "includeAssetIds"
                            )
                        ],
                        [
                            str(item)
                            for item in _list_input(
                                body.get("excludeAssetIds", []), "excludeAssetIds"
                            )
                        ],
                    ),
                    HTTPStatus.CREATED,
                )
            source_groups = body.get("sourceGroups")
            if source_groups is not None and not isinstance(source_groups, list):
                raise DomainError("VALIDATION_FAILED", "sourceGroups must be an array")
            normalized_groups = None
            if source_groups is not None:
                normalized_groups = []
                for item in source_groups:
                    if not isinstance(item, dict):
                        raise DomainError("VALIDATION_FAILED", "Every source group must be an object")
                    label = str(_required(item, "label")).strip()
                    if not label:
                        raise DomainError("VALIDATION_FAILED", "Source group labels may not be empty")
                    normalized_groups.append(
                        {
                            "label": label,
                            "assetIds": [
                                str(asset_id)
                                for asset_id in _list_input(item.get("assetIds", []), "assetIds")
                            ],
                        }
                    )
            return self.respond(
                APP.store.create_project(
                    str(body.get("name", "Untitled alignment")),
                    str(_required(body, "libraryId")),
                    [str(item) for item in _list_input(body.get("assetIds", []), "assetIds")],
                    normalized_groups,
                ),
                HTTPStatus.CREATED,
            )
        if path == "/api/v1/jobs/event-token":
            session_id, _session = self.session()
            return self.respond(APP.sessions.event_token(session_id), HTTPStatus.CREATED)
        parts = [part for part in path.split("/") if part]
        if len(parts) == 5 and parts[2] == "grants" and parts[4] == "revoke":
            return self.respond(APP.store.revoke_grant(parts[3]))
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "roots":
            return self.respond(
                APP.store.add_library_root(
                    parts[3],
                    str(_required(body, "grantId")),
                    str(body["label"]) if body.get("label") is not None else None,
                    _dict_input(body["timePolicyOverride"], "timePolicyOverride")
                    if body.get("timePolicyOverride") is not None
                    else None,
                ),
                HTTPStatus.CREATED,
            )
        if (
            len(parts) == 7
            and parts[2] == "libraries"
            and parts[4] == "roots"
            and parts[6] == "revoke"
        ):
            return self.respond(APP.store.revoke_library_root(parts[3], parts[5]))
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "scans":
            mode = str(body.get("mode", "INCREMENTAL"))
            limit = _int_input(body["limit"], "limit") if body.get("limit") is not None else None
            root_ids = [str(item) for item in _list_input(body.get("rootIds", []), "rootIds")]
            if limit is not None:
                mode = "BOUNDED"
            return self.respond(
                APP.start_scan(parts[3], mode, limit, root_ids or None), HTTPStatus.ACCEPTED
            )
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "time-policy":
            return self.respond(
                APP.store.update_library_time_policy(
                    parts[3],
                    str(_required(body, "timeZone")),
                    _int_input(body.get("dstFold", 0), "dstFold"),
                    str(body.get("nonexistentPolicy", "REJECT")),
                )
            )
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "cluster-jobs":
            return self.respond(
                APP.start_cluster_analysis(
                    parts[3],
                    _int_input(_required(body, "catalogRevision"), "catalogRevision"),
                    _int_input(body["eventGapUs"], "eventGapUs")
                    if body.get("eventGapUs") is not None
                    else None,
                    _int_input(body["sessionGapUs"], "sessionGapUs")
                    if body.get("sessionGapUs") is not None
                    else None,
                ),
                HTTPStatus.ACCEPTED,
            )
        if (
            len(parts) == 5
            and parts[2] == "cluster-generations"
            and parts[4] == "selection-preview"
        ):
            generation = APP.store.cluster_generation(parts[3])
            return self.respond(
                APP.store.project_selection_preview(
                    str(generation["libraryId"]),
                    parts[3],
                    [
                        str(item)
                        for item in _list_input(body.get("sessionIds", []), "sessionIds")
                    ],
                    [
                        str(item)
                        for item in _list_input(body.get("eventIds", []), "eventIds")
                    ],
                    [
                        str(item)
                        for item in _list_input(
                            body.get("includeAssetIds", []), "includeAssetIds"
                        )
                    ],
                    [
                        str(item)
                        for item in _list_input(
                            body.get("excludeAssetIds", []), "excludeAssetIds"
                        )
                    ],
                )
            )
        if len(parts) == 5 and parts[2] == "scans" and parts[4] == "cancel":
            return self.respond(APP.store.cancel_scan(parts[3]))
        if len(parts) == 6 and parts[2] == "media" and parts[4:] == ["provenance", "resolutions"]:
            return self.respond(
                APP.store.resolve_provenance(
                    parts[3],
                    str(_required(body, "field")),
                    _dict_input(body.get("resolution", {}), "resolution"),
                    str(body.get("rationale")) if body.get("rationale") is not None else None,
                    "local-user",
                ),
                HTTPStatus.CREATED,
            )
        if (
            len(parts) == 6
            and parts[2] == "projects"
            and parts[4:] == ["commands", "delta"]
        ):
            preview = query.get("preview", ["false"])[0].lower() == "true"
            return self.respond(
                APP.store.apply_project_delta_command(parts[3], body, preview)
            )
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "commands":
            preview = query.get("preview", ["false"])[0].lower() == "true"
            return self.respond(APP.store.apply_project_command(parts[3], body, preview))
        if (
            len(parts) == 5
            and parts[2] == "projects"
            and parts[4] == "alignment-proposal-acceptance-previews"
        ):
            return self.respond(
                APP.store.create_alignment_acceptance_preview(parts[3], body),
                HTTPStatus.CREATED,
            )
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "alignment-jobs":
            return self.respond(APP.start_alignment_analysis(parts[3]), HTTPStatus.ACCEPTED)
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "render-plans":
            if not APP.hash_slot.acquire(blocking=False):
                raise DomainError("JOB_STATE_CONFLICT", "Another full-hash render plan is being created")
            try:
                return self.respond(build_render_plan(APP.store, parts[3], body), HTTPStatus.CREATED)
            finally:
                APP.hash_slot.release()
        if len(parts) == 5 and parts[2] == "render-plans" and parts[4] == "review":
            return self.respond(
                APP.store.attest_review(
                    parts[3],
                    [str(item) for item in _list_input(body.get("acknowledgedWarnings", []), "acknowledgedWarnings")],
                ),
                HTTPStatus.CREATED,
            )
        if len(parts) == 5 and parts[2] == "render-plans" and parts[4] == "render":
            return self.respond(APP.render.start(parts[3]), HTTPStatus.ACCEPTED)
        if len(parts) == 5 and parts[2] == "jobs" and parts[4] == "cancel":
            job = APP.store.job(parts[3])
            if job["kind"] == "RENDER":
                return self.respond(APP.render.cancel(parts[3]))
            if job["kind"] == "SCAN":
                return self.respond(APP.store.cancel_scan(parts[3]))
            if job["status"] not in TERMINAL_JOB_STATES:
                return self.respond(APP.store.transition_job(parts[3], "CANCEL_REQUESTED", message="Cancellation requested"))
            return self.respond(job)
        raise DomainError("NOT_FOUND", "API resource not found")

    def stream_events(self, query: dict[str, list[str]]) -> None:
        after_header = self.headers.get("Last-Event-ID", "0")
        after = _int_input(query.get("after", [after_header or "0"])[0], "after")
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        deadline = time.monotonic() + 20
        heartbeat = time.monotonic()
        cursor = after
        minimum, latest = APP.store.event_bounds()
        if cursor and minimum and cursor < minimum - 1:
            reset = json.dumps({"minimumSequence": minimum, "latestSequence": latest}, separators=(",", ":"))
            self.wfile.write(f"id: {latest}\nevent: reset\ndata: {reset}\n\n".encode("utf-8"))
            self.wfile.flush()
            cursor = latest
        while time.monotonic() < deadline:
            wait_seconds = min(5 - (time.monotonic() - heartbeat), deadline - time.monotonic())
            events = APP.store.wait_for_events(cursor, max(0, wait_seconds), 1_000)
            for event in events:
                cursor = int(event["sequence"])
                data = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
                self.wfile.write(f"id: {cursor}\nevent: job\ndata: {data}\n\n".encode("utf-8"))
            if events:
                self.wfile.flush()
            if time.monotonic() - heartbeat >= 5:
                self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                self.wfile.flush()
                heartbeat = time.monotonic()

    def serve_static(self, request_path: str) -> None:
        relative = unquote(request_path).lstrip("/") or "index.html"
        target = (WEB / relative).resolve()
        if not target.is_relative_to(WEB.resolve()) or not target.is_file():
            target = WEB / "index.html"
        body = target.read_bytes()
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def _trusted_host(value: str, port: int) -> bool:
    try:
        parsed = urlparse(f"//{value}")
        host = parsed.hostname
        supplied_port = parsed.port
    except ValueError:
        return False
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    return supplied_port in {None, port}


def _required(body: dict[str, object], name: str) -> object:
    if name not in body or body[name] is None:
        raise DomainError("VALIDATION_FAILED", f"Missing required field: {name}")
    return body[name]


def _required_query(query: dict[str, list[str]], name: str) -> str:
    if not query.get(name):
        raise DomainError("VALIDATION_FAILED", f"Missing required query field: {name}")
    return query[name][0]


def _int_input(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise DomainError("VALIDATION_FAILED", f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise DomainError("VALIDATION_FAILED", f"{name} must be an integer") from error


def _list_input(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise DomainError("VALIDATION_FAILED", f"{name} must be an array")
    return value


def _dict_input(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DomainError("VALIDATION_FAILED", f"{name} must be an object")
    return value


def _trusted_origin(value: str, port: int) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == port


def _safe_error(error: Exception) -> str:
    if isinstance(error, DomainError):
        return str(error)[:300]
    if isinstance(error, ValueError):
        return str(error)[:300]
    if isinstance(error, OSError):
        return f"Operating system error ({error.errno or 'unknown'})"
    return type(error).__name__


def _timestamp_us(value: object) -> int | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1_000_000)
    except ValueError:
        return None


def add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".room-alignment")
    parser.add_argument("--no-open", action="store_true")


def serve(args: argparse.Namespace) -> int:
    try:
        if not ipaddress.ip_address(args.host).is_loopback:
            raise SystemExit("Room Alignment only supports loopback addresses")
    except ValueError:
        if args.host != "localhost":
            raise SystemExit("Room Alignment only supports loopback addresses")
    global APP
    APP = App(args.data_dir.expanduser())
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except BaseException:
        APP.close()
        raise
    server.daemon_threads = True
    actual_port = int(server.server_port)
    bootstrap_url = f"http://{args.host}:{actual_port}/bootstrap/{quote(APP.sessions.bootstrap_token, safe='')}"
    print(f"Room Alignment secure launch: {bootstrap_url}", flush=True)
    browser_timer: threading.Timer | None = None
    if not args.no_open:
        browser_timer = threading.Timer(0.4, lambda: webbrowser.open(bootstrap_url))
        browser_timer.daemon = True
        browser_timer.start()

    previous_sigterm = None

    def request_shutdown(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, request_shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        # shutdown() is only safe when called from a different thread than
        # serve_forever(). At this point the serving loop has already exited.
        server.server_close()
        APP.close()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Room Alignment locally")
    add_serve_arguments(parser)
    return serve(parser.parse_args(argv))


if __name__ == "__main__":
    main()
