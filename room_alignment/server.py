from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import threading
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .render import PreflightError, RenderManager, build_manifest, preflight
from .scanner import scan_library
from .store import Store


ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


class App:
    def __init__(self, data_dir: Path):
        self.store = Store(data_dir / "room-alignment.sqlite3")
        self.render = RenderManager(self.store)
        self.scans: dict[str, dict] = {}
        self.lock = threading.RLock()

    def start_scan(self, path: str, limit: int | None = None) -> str:
        root = Path(path).expanduser().resolve(strict=True)
        library_id = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        scan_id = uuid.uuid4().hex
        self.scans[scan_id] = {"id": scan_id, "status": "queued", "libraryId": library_id, "count": 0}

        def run() -> None:
            try:
                self.scans[scan_id]["status"] = "running"
                summary, records = scan_library(root, library_id, lambda _: self.scans[scan_id].update(count=self.scans[scan_id]["count"] + 1), limit)
                self.store.save_scan(summary, records)
                self.scans[scan_id] |= {"status": "complete", "summary": summary.to_dict()}
            except Exception as error:
                self.scans[scan_id] |= {"status": "failed", "error": str(error)}

        threading.Thread(target=run, daemon=True).start()
        return scan_id


APP: App


class Handler(BaseHTTPRequestHandler):
    server_version = "RoomAlignment/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print(json.dumps({"component": "http", "client": self.client_address[0], "message": format % args}))

    def json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 10_000_000:
            raise ValueError("Request too large")
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def trusted_local_request(self, require_origin: bool = False) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return False
        if require_origin:
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != self.server.server_port:
                    return False
        return True

    def respond(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error(self, error: Exception, status: int = 400) -> None:
        self.respond({"error": type(error).__name__, "message": str(error)}, status)

    def do_GET(self) -> None:
        try:
            if not self.trusted_local_request():
                return self.respond({"message": "Local Host header required"}, 403)
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/api/health":
                return self.respond({"ok": True, "version": "0.1.0", "ffmpeg": bool(os.environ.get("PATH"))})
            if path == "/api/libraries":
                return self.respond(APP.store.libraries())
            if path.startswith("/api/scans/"):
                return self.respond(APP.scans.get(path.rsplit("/", 1)[-1], {"status": "missing"}), 200)
            if path == "/api/media":
                library_id = query.get("libraryId", [""])[0]
                limit = min(2000, int(query.get("limit", ["500"])[0]))
                offset = int(query.get("offset", ["0"])[0])
                return self.respond(APP.store.media(library_id, limit, offset))
            if path == "/api/projects":
                return self.respond(APP.store.projects())
            if path.startswith("/api/projects/") and path.endswith("/manifest"):
                project_id = path.split("/")[3]
                project = APP.store.project(project_id)
                if not project:
                    return self.respond({"message": "Project not found"}, 404)
                return self.respond(build_manifest(APP.store, project))
            if path.startswith("/api/projects/") and path.endswith("/preflight"):
                project_id = path.split("/")[3]
                project = APP.store.project(project_id)
                if not project:
                    return self.respond({"message": "Project not found"}, 404)
                return self.respond(preflight(APP.store, project))
            if path.startswith("/api/projects/"):
                project = APP.store.project(path.rsplit("/", 1)[-1])
                return self.respond(project or {"message": "Project not found"}, 200 if project else 404)
            if path.startswith("/api/render/"):
                job = APP.store.job(path.rsplit("/", 1)[-1])
                return self.respond(job or {"message": "Render job not found"}, 200 if job else 404)
            return self.serve_static(path)
        except Exception as error:
            self.error(error)

    def do_POST(self) -> None:
        try:
            if not self.trusted_local_request(require_origin=True):
                return self.respond({"message": "Same-origin local request required"}, 403)
            path = urlparse(self.path).path
            body = self.json_body()
            if path == "/api/scans":
                scan_id = APP.start_scan(body["path"], body.get("limit"))
                return self.respond({"scanId": scan_id}, HTTPStatus.ACCEPTED)
            if path == "/api/projects":
                required = {"id", "name", "libraryId"}
                if not required.issubset(body):
                    raise ValueError("Project requires id, name, and libraryId")
                APP.store.save_project(body)
                return self.respond({"saved": True, "id": body["id"]}, HTTPStatus.CREATED)
            if path.startswith("/api/projects/") and path.endswith("/render"):
                project_id = path.split("/")[3]
                project = APP.store.project(project_id)
                if not project:
                    return self.respond({"message": "Project not found"}, 404)
                job_id = APP.render.start(project, Path(body["outputPath"]), bool(body.get("lossless")))
                return self.respond({"jobId": job_id}, HTTPStatus.ACCEPTED)
            if path.startswith("/api/render/") and path.endswith("/cancel"):
                job_id = path.split("/")[3]
                return self.respond({"canceled": APP.render.cancel(job_id)})
            return self.respond({"message": "Not found"}, 404)
        except FileNotFoundError:
            self.error(ValueError("Folder does not exist"))
        except (ValueError, KeyError, PreflightError) as error:
            self.error(error)
        except Exception as error:
            self.error(error, 500)

    def serve_static(self, request_path: str) -> None:
        relative = unquote(request_path).lstrip("/") or "index.html"
        target = (WEB / relative).resolve()
        if not target.is_relative_to(WEB.resolve()) or not target.is_file():
            target = WEB / "index.html"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Room Alignment locally")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".room-alignment")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    global APP
    APP = App(args.data_dir.expanduser())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Room Alignment running at {url}")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
