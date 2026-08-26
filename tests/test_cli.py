from __future__ import annotations

import io
import json
import subprocess
import sys
import tomllib
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from room_alignment import __version__
from room_alignment import cli
from room_alignment.lifecycle import stop
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
            self.assertEqual((data_dir / "application.lock").read_text(), "")

    def test_stop_is_idempotent_without_a_running_application(self):
        with TemporaryDirectory() as temporary:
            self.assertEqual(
                stop(Path(temporary)),
                {"status": "NOT_RUNNING", "forced": False},
            )

    def test_stop_command_gracefully_stops_validated_state_owner(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "room_alignment",
                    "serve",
                    "--no-open",
                    "--port",
                    "0",
                    "--data-dir",
                    str(state),
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                self.assertIn("Room Alignment secure launch:", process.stdout.readline())
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["stop", "--data-dir", str(state)]), 0)
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {"status": "STOPPED", "forced": False},
                )
                self.assertEqual(process.wait(timeout=5), 0)

                reopened = App(state)
                reopened.close()
                self.assertEqual((state / "application.lock").read_text(), "")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_stop_refuses_a_contended_lock_without_room_alignment_identity(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            lock_path = state / "application.lock"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl, pathlib, sys, time; "
                        "f=pathlib.Path(sys.argv[1]).open('w+'); "
                        "f.write('{}'); f.flush(); "
                        "fcntl.flock(f.fileno(), fcntl.LOCK_EX); "
                        "print('ready', flush=True); time.sleep(30)"
                    ),
                    str(lock_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(ValueError, "not owned by Room Alignment"):
                    stop(state)
                self.assertIsNone(process.poll())
            finally:
                process.kill()
                process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
