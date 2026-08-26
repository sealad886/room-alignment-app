from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from room_alignment import server as server_module
from room_alignment.domain import DomainError
from room_alignment.models import MediaRecord


class ServerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.source.mkdir()
        self.output.mkdir()
        self.app = server_module.App(self.root / "state")
        self.addCleanup(self.app.close)
        had_previous_app = hasattr(server_module, "APP")
        previous_app = getattr(server_module, "APP", None)

        def restore_app() -> None:
            if had_previous_app:
                server_module.APP = previous_app
            elif hasattr(server_module, "APP"):
                del server_module.APP

        self.addCleanup(restore_app)
        server_module.APP = self.app
        try:
            self.server = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
        except PermissionError as error:
            self.skipTest(f"loopback sockets unavailable in this execution boundary: {error}")
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_port

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        cookie: str | None = None,
        csrf: str | None = None,
        origin: str | None = None,
        host: str | None = None,
        range_header: str | None = None,
    ) -> tuple[int, dict[str, str], object]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if origin:
            headers["Origin"] = origin
            headers["Sec-Fetch-Site"] = "same-origin" if origin.endswith(str(self.port)) else "cross-site"
        if range_header:
            headers["Range"] = range_header
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        content_type = response_headers.get("content-type", "")
        payload = json.loads(raw) if raw and "json" in content_type else raw.decode()
        connection.close()
        return response.status, response_headers, payload

    def bootstrap(self) -> tuple[str, str]:
        token = self.app.sessions.bootstrap_token
        status, headers, _payload = self.request("GET", f"/bootstrap/{token}")
        self.assertEqual(303, status)
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _headers, session = self.request("GET", "/api/v1/session", cookie=cookie)
        self.assertEqual(200, status)
        return cookie, session["csrfToken"]

    def test_sensitive_api_requires_bootstrapped_session(self) -> None:
        status, _headers, payload = self.request("GET", "/api/v1/libraries")
        self.assertEqual(401, status)
        self.assertEqual("UNAUTHENTICATED", payload["error"]["code"])
        self.assertNotIn(str(self.source), json.dumps(payload))

    def test_host_origin_and_csrf_boundaries(self) -> None:
        status, _headers, payload = self.request("GET", "/api/health", host="attacker.invalid")
        self.assertEqual(403, status)
        self.assertEqual("FORBIDDEN", payload["error"]["code"])

        cookie, csrf = self.bootstrap()
        request_body = {"path": str(self.source), "role": "READ_ONLY_SOURCE"}

        status, _headers, payload = self.request("POST", "/api/v1/grants", body=request_body, cookie=cookie)
        self.assertEqual(403, status)
        self.assertEqual("FORBIDDEN", payload["error"]["code"])

        status, _headers, payload = self.request(
            "POST",
            "/api/v1/grants",
            body=request_body,
            cookie=cookie,
            csrf=csrf,
            origin="http://attacker.invalid",
        )
        self.assertEqual(403, status)
        self.assertEqual("FORBIDDEN", payload["error"]["code"])

        status, _headers, grant = self.request(
            "POST",
            "/api/v1/grants",
            body=request_body,
            cookie=cookie,
            csrf=csrf,
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(201, status)
        self.assertEqual("READ_ONLY_SOURCE", grant["role"])
        self.assertNotIn("root", grant)

        status, _headers, payload = self.request(
            "GET",
            "/api/v1/libraries",
            cookie=cookie,
            origin="http://attacker.invalid",
        )
        self.assertEqual(403, status)
        self.assertEqual("FORBIDDEN", payload["error"]["code"])

    def test_bootstrap_token_is_one_time_and_redacted_from_http_log(self) -> None:
        token = self.app.sessions.bootstrap_token
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            status, _headers, _payload = self.request("GET", f"/bootstrap/{token}")
        self.assertEqual(303, status)
        self.assertNotIn(token, capture.getvalue())

        status, _headers, payload = self.request("GET", f"/bootstrap/{token}")
        self.assertEqual(401, status)
        self.assertEqual("UNAUTHENTICATED", payload["error"]["code"])

    def test_event_tokens_are_session_bound(self) -> None:
        cookie, csrf = self.bootstrap()
        status, _headers, token = self.request(
            "POST", "/api/v1/jobs/event-token", body={}, cookie=cookie, csrf=csrf
        )
        self.assertEqual(201, status)
        self.assertGreaterEqual(token["expiresInSeconds"], 1)

        status, _headers, payload = self.request("GET", f"/api/v1/events?token={token['token']}")
        self.assertEqual(401, status)
        self.assertEqual("UNAUTHENTICATED", payload["error"]["code"])

        other_session = "other-session"
        self.app.sessions.sessions[other_session] = {"csrf": "other-csrf", "created": time.monotonic()}
        status, _headers, payload = self.request(
            "GET",
            f"/api/v1/events?token={token['token']}",
            cookie=f"{server_module.SESSION_COOKIE}={other_session}",
        )
        self.assertEqual(401, status)
        self.assertEqual("UNAUTHENTICATED", payload["error"]["code"])

    def test_user_canceled_analysis_finishes_as_canceled(self) -> None:
        job = self.app.store.create_job("ALIGNMENT_ANALYSIS", message="Queued")
        self.app.store.transition_job(job["id"], "CANCEL_REQUESTED", 0, "Cancellation requested")
        self.app._finish_analysis_error(job["id"], DomainError("JOB_STATE_CONFLICT", "Job was canceled"))
        self.assertEqual(self.app.store.job(job["id"])["status"], "CANCELED")

    def test_server_enforces_session_expiry_not_only_cookie_age(self) -> None:
        cookie, _csrf = self.bootstrap()
        session_id = cookie.split("=", 1)[1]
        self.app.sessions.sessions[session_id]["created"] = time.monotonic() - server_module.SESSION_TTL_SECONDS - 1
        status, _headers, payload = self.request("GET", "/api/v1/libraries", cookie=cookie)
        self.assertEqual(401, status)
        self.assertEqual("UNAUTHENTICATED", payload["error"]["code"])

    def test_normative_contract_files_are_served_as_json(self) -> None:
        cookie, _csrf = self.bootstrap()
        status, _headers, openapi = self.request("GET", "/api/v1/openapi.json", cookie=cookie)
        self.assertEqual(200, status)
        self.assertEqual(openapi["openapi"], "3.1.0")
        status, _headers, schema = self.request("GET", "/api/v1/contracts/manifest.schema.json", cookie=cookie)
        self.assertEqual(200, status)
        self.assertEqual(schema["title"], "Room Alignment provenance manifest v1")

    def test_multi_root_library_routes_and_selected_root_scan(self) -> None:
        cookie, csrf = self.bootstrap()
        second_source = self.root / "second-source"
        second_source.mkdir()
        grants = []
        for source in (self.source, second_source):
            status, _headers, grant = self.request(
                "POST",
                "/api/v1/grants",
                body={"path": str(source), "role": "READ_ONLY_SOURCE"},
                cookie=cookie,
                csrf=csrf,
            )
            self.assertEqual(status, 201)
            grants.append(grant)
        status, _headers, library = self.request(
            "POST",
            "/api/v1/libraries",
            body={"name": "Several folders", "timeZone": "Europe/Dublin"},
            cookie=cookie,
            csrf=csrf,
        )
        self.assertEqual(status, 201)
        roots = []
        for index, grant in enumerate(grants):
            status, _headers, root = self.request(
                "POST",
                f"/api/v1/libraries/{library['id']}/roots",
                body={"grantId": grant["id"], "label": f"Folder {index + 1}"},
                cookie=cookie,
                csrf=csrf,
            )
            self.assertEqual(status, 201)
            roots.append(root)
        status, _headers, listed = self.request(
            "GET", f"/api/v1/libraries/{library['id']}/roots", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in listed], [item["id"] for item in roots])

        status, _headers, scan = self.request(
            "POST",
            f"/api/v1/libraries/{library['id']}/scans",
            body={"mode": "FULL", "rootIds": [roots[1]["id"]]},
            cookie=cookie,
            csrf=csrf,
        )
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 2
        while scan["status"] not in {"SUCCEEDED", "FAILED", "CANCELED"} and time.monotonic() < deadline:
            time.sleep(0.02)
            status, _headers, scan = self.request(
                "GET", f"/api/v1/scans/{scan['id']}", cookie=cookie
            )
            self.assertEqual(status, 200)
        self.assertEqual(scan["status"], "SUCCEEDED")
        self.assertEqual(scan["rootIds"], [roots[1]["id"]])
        self.assertTrue(scan["roots"][0]["fullTraversalCompleted"])

    def test_source_preview_is_authenticated_read_only_and_range_capable(self) -> None:
        source_file = self.source / "clip.mp4"
        source_file.write_bytes(b"0123456789")
        grant = self.app.store.create_grant(self.source, "READ_ONLY_SOURCE")
        library = self.app.store.create_library(grant["id"])
        scan = self.app.store.begin_scan(library["id"], "FULL")
        self.app.store.save_media_batch(
            scan["id"],
            [MediaRecord("media", library["id"], "clip.mp4", 10, source_file.stat().st_mtime_ns)],
        )
        self.app.store.finish_scan(scan["id"], "SUCCEEDED", {"videos": 1})

        status, _headers, payload = self.request("GET", "/api/v1/media/media/preview")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "UNAUTHENTICATED")

        cookie, _csrf = self.bootstrap()
        status, headers, payload = self.request(
            "GET", "/api/v1/media/media/preview", cookie=cookie, range_header="bytes=2-5"
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-range"], "bytes 2-5/10")
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(payload, "2345")
        self.assertEqual(source_file.read_bytes(), b"0123456789")

        status, headers, payload = self.request(
            "HEAD", "/api/v1/media/media/preview", cookie=cookie, range_header="bytes=0-3"
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-range"], "bytes 0-3/10")
        self.assertEqual(payload, "")

    def test_malformed_client_values_return_stable_validation_errors(self) -> None:
        cookie, csrf = self.bootstrap()
        requests = [
            ("POST", "/api/v1/grants", {}, csrf),
            ("GET", "/api/v1/libraries/library/media?limit=bad", None, None),
            ("GET", "/api/v1/projects/project/revisions/not-an-integer", None, None),
            ("GET", "/api/v1/projects/project/program-at?outputUs=bad", None, None),
        ]
        for method, path, body, request_csrf in requests:
            with self.subTest(path=path):
                status, _headers, payload = self.request(
                    method, path, body=body, cookie=cookie, csrf=request_csrf
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "VALIDATION_FAILED")
                self.assertFalse(payload["error"]["retryable"])


class WorkerBackpressureTests(unittest.TestCase):
    def test_analysis_jobs_have_a_two_job_backpressure_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            app = server_module.App(root / "state")
            release = threading.Event()
            try:
                grant = app.store.create_grant(source, "READ_ONLY_SOURCE")
                library = app.store.create_library(grant["id"])
                original_check = app._raise_if_job_stopping

                def hold(job_id):
                    release.wait(2)
                    return original_check(job_id)

                with patch.object(app, "_raise_if_job_stopping", side_effect=hold):
                    app.start_cluster_analysis(library["id"])
                    app.start_cluster_analysis(library["id"])
                    with self.assertRaisesRegex(DomainError, "two analysis"):
                        app.start_cluster_analysis(library["id"])
                    release.set()
                    for thread in list(app.analysis_threads.values()):
                        thread.join(timeout=2)
            finally:
                release.set()
                app.analysis_threads.clear()
                app.analysis_reserved.clear()
                app.close()


if __name__ == "__main__":
    unittest.main()
