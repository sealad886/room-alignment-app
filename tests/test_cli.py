from __future__ import annotations

import io
import json
import tomllib
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from room_alignment import __version__
from room_alignment import cli
from room_alignment.server import App, CONTRACT, CONTRACTS, WEB, serve


ROOT = Path(__file__).resolve().parent.parent


class InstallableCliTests(unittest.TestCase):
    def test_project_and_runtime_versions_match(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], __version__)

    def test_version_command_uses_installed_entrypoint_version(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
            cli.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"room-alignment {__version__}")

    def test_legacy_launch_options_dispatch_to_serve(self):
        with patch("room_alignment.cli.serve", return_value=0) as run:
            self.assertEqual(cli.main(["--no-open", "--port", "0"]), 0)
        args = run.call_args.args[0]
        self.assertTrue(args.no_open)
        self.assertEqual(args.port, 0)

    def test_doctor_reports_resources_without_absolute_paths(self):
        tool = {"available": True, "supported": True, "version": "tool version 9.0"}
        output = io.StringIO()
        with patch("room_alignment.cli._tool_status", return_value=tool), redirect_stdout(output):
            self.assertEqual(cli.doctor(), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["resources"], {"frontend": True, "openapi": True, "schemas": True})
        self.assertNotIn(str(ROOT), output.getvalue())

    def test_doctor_rejects_media_tool_below_supported_floor(self):
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "ffmpeg version 5.1\n", "stderr": ""},
        )()
        with patch("room_alignment.cli.shutil.which", return_value="ffmpeg"), patch(
            "room_alignment.cli.subprocess.run", return_value=completed
        ):
            self.assertEqual(
                cli._tool_status("ffmpeg"),
                {"available": True, "supported": False, "version": "ffmpeg version 5.1"},
            )

    def test_runtime_resources_are_available(self):
        self.assertTrue((WEB / "index.html").is_file())
        self.assertTrue(CONTRACT.is_file())
        self.assertTrue((CONTRACTS / "manifest.schema.json").is_file())

    def test_port_bind_failure_releases_state_directory_lock(self):
        with TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "state"
            args = Namespace(host="127.0.0.1", port=8765, data_dir=data_dir, no_open=True)
            with patch("room_alignment.server.ThreadingHTTPServer", side_effect=OSError("busy")):
                with self.assertRaisesRegex(OSError, "busy"):
                    serve(args)
            reopened = App(data_dir)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
