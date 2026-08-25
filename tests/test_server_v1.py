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

from room_alignment import server as server_module


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


if __name__ == "__main__":
    unittest.main()
