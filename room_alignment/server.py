from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import mimetypes
import os
import secrets
import threading
import time
import uuid
import webbrowser
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .domain import DomainError, digest_json, program_at
from .render import CanonicalRenderManager, RenderManager, build_render_plan
from .scanner import iter_scan_records
from .store import Store, TERMINAL_JOB_STATES


ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
CONTRACT = ROOT / "contracts" / "openapi.json"
CONTRACTS = ROOT / "contracts"
MAX_BODY = 2_000_000
SESSION_COOKIE = "ra_session"
SESSION_TTL_SECONDS = 43_200


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
            token = secrets.token_urlsafe(32)
            expires_at = time.monotonic() + 60
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
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.data_dir / "application.lock").open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lock_file.close()
            raise RuntimeError("Another Room Alignment process owns this state directory") from error
        self.store = Store(self.data_dir / "room-alignment.sqlite3")
        self.legacy_render = RenderManager(self.store)
        self.render = CanonicalRenderManager(self.store)
        self.sessions = SessionManager()
        self.scan_threads: dict[str, threading.Thread] = {}
        self.analysis_threads: dict[str, threading.Thread] = {}
        self.lock = threading.RLock()
        self.closing = False

    def close(self) -> None:
        self.closing = True
        for scan_id in list(self.scan_threads):
            try:
                self.store.cancel_scan(scan_id)
            except DomainError:
                pass
        self.render.shutdown()
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_file.close()

    def start_scan(self, library_id: str, mode: str, limit: int | None = None) -> dict[str, object]:
        scan = self.store.begin_scan(library_id, mode, limit)

        def run() -> None:
            cameras: set[str] = set()
            date_groups: dict[str, int] = {}
            warnings = 0
            batch = []
            try:
                root = self.store.library_root(library_id)
                for record in iter_scan_records(
                    root,
                    library_id,
                    mode=mode,
                    existing_lookup=lambda path: self.store.existing_media_by_path(library_id, path),
                    canceled=lambda: self.closing or self.store.scan_cancel_requested(scan["id"]),
                    max_files=limit,
                ):
                    if self.closing or self.store.scan_cancel_requested(scan["id"]):
                        self.store.finish_scan(
                            scan["id"],
                            "CANCELED",
                            {"videos": self.store.scan(scan["id"])["videos"]},
                            "Scan canceled; visited records retained",
                        )
                        return
                    if record.camera:
                        cameras.add(record.camera)
                    if record.captured_at:
                        date = str(record.captured_at)[:10]
                        date_groups[date] = date_groups.get(date, 0) + 1
                    warnings += int(bool(record.warning))
                    batch.append(record)
                    self.store.scan_progress(scan["id"], warning=bool(record.warning), message="Indexing media")
                    if len(batch) >= 50:
                        self.store.save_media_batch(scan["id"], batch)
                        batch.clear()
                if batch:
                    self.store.save_media_batch(scan["id"], batch)
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
                }
                self.store.finish_scan(scan["id"], "SUCCEEDED", summary, "Scan complete")
            except Exception as error:
                try:
                    self.store.finish_scan(
                        scan["id"],
                        "FAILED",
                        {"videos": self.store.scan(scan["id"])["videos"]},
                        _safe_error(error),
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
        project = self.store.project(project_id)
        job = self.store.create_job("ALIGNMENT_ANALYSIS", project_id=project_id, message="Analysis queued")

        def run() -> None:
            try:
                self.store.transition_job(job["id"], "RUNNING", 0.1, "Comparing timestamp evidence")
                assets = self.store.media_records(item["assetId"] for item in project["clips"])
                reference = next(
                    (
                        clip
                        for clip in project["clips"]
                        if next(
                            (
                                source.get("reference")
                                for source in project["logicalSources"]
                                if source["id"] == clip["logicalSourceId"]
                            ),
                            False,
                        )
                    ),
                    project["clips"][0],
                )
                reference_time = _timestamp_us(assets.get(reference["assetId"], {}).get("captured_at"))
                created = []
                if reference_time is not None:
                    for clip in project["clips"]:
                        if clip["id"] == reference["id"]:
                            continue
                        captured = _timestamp_us(assets.get(clip["assetId"], {}).get("captured_at"))
                        if captured is None:
                            continue
                        suggestion = self.store.save_suggestion(
                            {
                                "projectId": project_id,
                                "libraryId": project["libraryId"],
                                "kind": "ALIGNMENT",
                                "inputDigest": digest_json(
                                    {
                                        "projectRevision": project["revision"],
                                        "assets": [reference["assetId"], clip["assetId"]],
                                        "fingerprints": [
                                            assets[reference["assetId"]].get("fingerprint", {}),
                                            assets[clip["assetId"]].get("fingerprint", {}),
                                        ],
                                    }
                                ),
                                "algorithm": "timestamp-evidence",
                                "algorithmVersion": "1",
                                "projectRevision": project["revision"],
                                "confidence": 0.55,
                                "clipId": clip["id"],
                                "sync": {
                                    "anchorSourceUs": 0,
                                    "anchorOutputUs": captured - reference_time,
                                    "ratePpm": 0,
                                },
                                "evidence": ["resolved-or-naive captured timestamp"],
                                "limitations": [
                                    "Clock error and timezone ambiguity are not corrected",
                                    "Manual verification remains authoritative",
                                ],
                            }
                        )
                        created.append(suggestion["id"])
                self.store.transition_job(
                    job["id"],
                    "SUCCEEDED",
                    1,
                    f"Created {len(created)} non-mutating suggestions",
                    result={"suggestionIds": created},
                )
            except Exception as error:
                self.store.transition_job(
                    job["id"], "FAILED", 0, _safe_error(error), error_code=getattr(error, "code", "INTERNAL_ERROR")
                )
            finally:
                with self.lock:
                    self.analysis_threads.pop(job["id"], None)

        thread = threading.Thread(target=run, daemon=True, name=f"analysis-{job['id']}")
        with self.lock:
            self.analysis_threads[job["id"]] = thread
        thread.start()
        return job

    def start_cluster_analysis(self, library_id: str) -> dict[str, object]:
        self.store.library(library_id)
        job = self.store.create_job("CLUSTER_ANALYSIS", library_id=library_id, message="Clustering queued")

        def run() -> None:
            try:
                self.store.transition_job(job["id"], "RUNNING", 0.05, "Grouping timestamp evidence")
                records = self.store.clustering_media(library_id)
                groups: list[list[dict[str, object]]] = []
                current: list[dict[str, object]] = []
                start_us: int | None = None
                for record in records:
                    captured_us = _timestamp_us(record.get("captured_at"))
                    if captured_us is None:
                        continue
                    if start_us is None or captured_us - start_us <= 120_000_000:
                        current.append(record)
                        start_us = captured_us if start_us is None else start_us
                    else:
                        groups.append(current)
                        current = [record]
                        start_us = captured_us
                if current:
                    groups.append(current)

                created: list[str] = []
                for group in groups:
                    source_ids = sorted(
                        {
                            str(record.get("sourceCandidateId"))
                            for record in group
                            if record.get("sourceCandidateId")
                        }
                    )
                    if len(source_ids) < 2:
                        continue
                    inputs = [
                        {"assetId": record["id"], "fingerprint": record.get("fingerprint", {})}
                        for record in group
                    ]
                    suggestion = self.store.save_suggestion(
                        {
                            "libraryId": library_id,
                            "kind": "CLUSTER",
                            "inputDigest": digest_json(inputs),
                            "algorithm": "timestamp-window",
                            "algorithmVersion": "1",
                            "config": {"windowUs": 120_000_000},
                            "confidence": 0.4,
                            "assetIds": [str(record["id"]) for record in group],
                            "sourceCandidateIds": source_ids,
                            "evidence": ["captured timestamp evidence within a two-minute window"],
                            "limitations": [
                                "Camera clocks may differ",
                                "The group is only a project-membership suggestion",
                            ],
                        }
                    )
                    created.append(suggestion["id"])
                self.store.transition_job(
                    job["id"],
                    "SUCCEEDED",
                    1,
                    f"Created {len(created)} non-mutating cluster suggestions",
                    result={"suggestionIds": created},
                )
            except Exception as error:
                self.store.transition_job(
                    job["id"], "FAILED", 0, _safe_error(error), error_code=getattr(error, "code", "INTERNAL_ERROR")
                )
            finally:
                with self.lock:
                    self.analysis_threads.pop(job["id"], None)

        thread = threading.Thread(target=run, daemon=True, name=f"cluster-{job['id']}")
        with self.lock:
            self.analysis_threads[job["id"]] = thread
        thread.start()
        return job


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
                return self.respond({"ok": True, "version": "0.2.0"})
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
        except BrokenPipeError:
            return
        except Exception as error:
            self.error(error)

    def get_api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/v1/system":
            return self.respond(
                {
                    "version": "0.2.0",
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
            if name not in {"api.schema.json", "domain.schema.json", "commands.schema.json", "manifest.schema.json"}:
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
                    int(query.get("limit", ["200"])[0]),
                    cursor,
                    int(generation) if generation is not None else None,
                )
            )
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "cluster-suggestions":
            return self.respond(APP.store.library_suggestions(parts[3]))
        if len(parts) == 4 and parts[2] == "media":
            media = APP.store.media_record(parts[3])
            media["resolutions"] = APP.store.provenance_resolutions(parts[3])
            return self.respond(media)
        if len(parts) == 6 and parts[2] == "media" and parts[4:] == ["provenance", "resolutions"]:
            return self.respond(APP.store.provenance_resolutions(parts[3], query.get("field", [None])[0]))
        if len(parts) == 4 and parts[2] == "projects":
            return self.respond(APP.store.project(parts[3]))
        if len(parts) == 6 and parts[2] == "projects" and parts[4] == "revisions":
            return self.respond(APP.store.project_revision(parts[3], int(parts[5])))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "program":
            return self.respond(APP.store.compiled_project(parts[3]))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "program-at":
            compiled = APP.store.compiled_project(parts[3])
            return self.respond(program_at(compiled, int(query.get("outputUs", ["0"])[0])))
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
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self.request_id())
        self.end_headers()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                self.wfile.write(chunk)

    def do_POST(self) -> None:
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

    def post_api(self, path: str, query: dict[str, list[str]], body: dict[str, object]) -> None:
        if path == "/api/v1/grants":
            return self.respond(
                APP.store.create_grant(Path(str(body["path"])), str(body["role"])), HTTPStatus.CREATED
            )
        if path == "/api/v1/libraries":
            return self.respond(
                APP.store.create_library(
                    str(body["sourceGrantId"]),
                    str(body.get("timeZone", "UTC")),
                    int(body.get("dstFold", 0)),
                    str(body.get("nonexistentPolicy", "REJECT")),
                ),
                HTTPStatus.CREATED,
            )
        if path == "/api/v1/projects":
            return self.respond(
                APP.store.create_project(
                    str(body.get("name", "Untitled alignment")),
                    str(body["libraryId"]),
                    [str(item) for item in body.get("assetIds", [])],
                ),
                HTTPStatus.CREATED,
            )
        if path == "/api/v1/jobs/event-token":
            session_id, _session = self.session()
            return self.respond(APP.sessions.event_token(session_id), HTTPStatus.CREATED)
        parts = [part for part in path.split("/") if part]
        if len(parts) == 5 and parts[2] == "grants" and parts[4] == "revoke":
            return self.respond(APP.store.revoke_grant(parts[3]))
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "scans":
            mode = str(body.get("mode", "INCREMENTAL"))
            limit = int(body["limit"]) if body.get("limit") is not None else None
            if limit is not None:
                mode = "BOUNDED"
            return self.respond(APP.start_scan(parts[3], mode, limit), HTTPStatus.ACCEPTED)
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "time-policy":
            return self.respond(
                APP.store.update_library_time_policy(
                    parts[3],
                    str(body["timeZone"]),
                    int(body.get("dstFold", 0)),
                    str(body.get("nonexistentPolicy", "REJECT")),
                )
            )
        if len(parts) == 5 and parts[2] == "libraries" and parts[4] == "cluster-jobs":
            return self.respond(APP.start_cluster_analysis(parts[3]), HTTPStatus.ACCEPTED)
        if len(parts) == 5 and parts[2] == "scans" and parts[4] == "cancel":
            return self.respond(APP.store.cancel_scan(parts[3]))
        if len(parts) == 6 and parts[2] == "media" and parts[4:] == ["provenance", "resolutions"]:
            return self.respond(
                APP.store.resolve_provenance(
                    parts[3],
                    str(body["field"]),
                    dict(body.get("resolution") or {}),
                    str(body.get("rationale")) if body.get("rationale") is not None else None,
                    "local-user",
                ),
                HTTPStatus.CREATED,
            )
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "commands":
            preview = query.get("preview", ["false"])[0].lower() == "true"
            return self.respond(APP.store.apply_project_command(parts[3], body, preview))
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "alignment-jobs":
            return self.respond(APP.start_alignment_analysis(parts[3]), HTTPStatus.ACCEPTED)
        if len(parts) == 5 and parts[2] == "projects" and parts[4] == "render-plans":
            return self.respond(build_render_plan(APP.store, parts[3], body), HTTPStatus.CREATED)
        if len(parts) == 5 and parts[2] == "render-plans" and parts[4] == "review":
            return self.respond(
                APP.store.attest_review(parts[3], [str(item) for item in body.get("acknowledgedWarnings", [])]),
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
        after = int(query.get("after", [after_header or "0"])[0])
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
            events = APP.store.events(cursor, 1_000)
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
            time.sleep(0.25)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Room Alignment locally")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".room-alignment")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    try:
        if not ipaddress.ip_address(args.host).is_loopback:
            raise SystemExit("Room Alignment only supports loopback addresses")
    except ValueError:
        if args.host != "localhost":
            raise SystemExit("Room Alignment only supports loopback addresses")
    global APP
    APP = App(args.data_dir.expanduser())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    bootstrap_url = f"http://{args.host}:{args.port}/bootstrap/{quote(APP.sessions.bootstrap_token, safe='')}"
    print(f"Room Alignment secure launch: {bootstrap_url}")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(bootstrap_url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        # shutdown() is only safe when called from a different thread than
        # serve_forever(). At this point the serving loop has already exited.
        server.server_close()
        APP.close()


if __name__ == "__main__":
    main()
