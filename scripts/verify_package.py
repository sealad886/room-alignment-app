from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
import zipfile
from pathlib import Path


LAUNCH_PATTERN = re.compile(r"^Room Alignment secure launch: (http://\S+)$")
REQUIRED_MEMBERS = {
    "room_alignment/web/index.html",
    "room_alignment/web/app.js",
    "room_alignment/web/api-client.js",
    "room_alignment/web/styles.css",
    "room_alignment/contracts/openapi.json",
    "room_alignment/contracts/api.schema.json",
    "room_alignment/contracts/commands.schema.json",
    "room_alignment/contracts/domain.schema.json",
    "room_alignment/contracts/manifest.schema.json",
}


def _read_launch_url(process: subprocess.Popen[str], timeout: float = 10) -> str:
    if process.stdout is None:
        raise RuntimeError("Server stdout is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read()[:500] if process.stderr else ""
                raise RuntimeError(f"Server exited before launch: {stderr}")
            for key, _mask in selector.select(timeout=min(0.25, deadline - time.monotonic())):
                line = key.fileobj.readline().strip()
                match = LAUNCH_PATTERN.match(line)
                if match:
                    return match.group(1)
    finally:
        selector.close()
    raise TimeoutError("Timed out waiting for secure launch URL")


def _stop(process: subprocess.Popen[str]) -> None:
    """
    Stop the server process gracefully and verify successful termination.
    
    Parameters:
    	process (subprocess.Popen[str]): The server process to stop.
    
    Raises:
    	RuntimeError: If the process does not stop after termination or exits with a nonzero status.
    """
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError("Installed server did not stop after SIGTERM")
    if process.returncode != 0:
        stderr = process.stderr.read()[:500] if process.stderr else ""
        raise RuntimeError(f"Installed server exited with {process.returncode}: {stderr}")


def _stop_via_cli(
    command: Path,
    state_dir: Path,
    cwd: Path,
    environment: dict[str, str],
    process: subprocess.Popen[str],
) -> None:
    """
    Stop the installed server through its command-line interface and verify a graceful shutdown.
    
    Parameters:
        command (Path): Path to the installed command.
        state_dir (Path): Server data directory passed to the stop command.
        cwd (Path): Working directory for the command.
        environment (dict[str, str]): Environment variables for the command.
        process (subprocess.Popen[str]): Running server process to verify after stopping.
    """
    result = subprocess.run(
        [str(command), "stop", "--data-dir", str(state_dir)],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Installed stop command failed: {result.stderr[:500]}")
    payload = json.loads(result.stdout)
    if payload != {"forced": False, "status": "STOPPED"}:
        raise RuntimeError(f"Installed stop command returned an unexpected result: {payload}")
    if process.wait(timeout=5) != 0:
        raise RuntimeError("Installed server exited unsuccessfully after stop command")


def _launch(command: Path, state_dir: Path, cwd: Path, environment: dict[str, str]) -> subprocess.Popen[str]:
    """
    Start the installed server on localhost using an ephemeral port.
    
    Parameters:
    	command (Path): Path to the server command.
    	state_dir (Path): Directory used for server state and data.
    	cwd (Path): Working directory for the server process.
    	environment (dict[str, str]): Environment variables for the server process.
    
    Returns:
    	subprocess.Popen[str]: The running server process.
    """
    return subprocess.Popen(
        [str(command), "serve", "--no-open", "--host", "127.0.0.1", "--port", "0", "--data-dir", str(state_dir)],
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def verify(wheel: Path) -> dict[str, object]:
    """
    Verify that an installable Room Alignment wheel functions independently of the source tree.
    
    Parameters:
    	wheel (Path): Path to the wheel file to validate and test.
    
    Returns:
    	dict[str, object]: JSON-compatible verification results, including the wheel
    	hash, version, readiness, endpoint status, shutdown status, state integrity,
    	and lock reuse.
    
    Raises:
    	RuntimeError: If the wheel is missing required resources or any installation,
    	runtime, endpoint, shutdown, or state-integrity check fails.
    """
    wheel = wheel.resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
    missing = sorted(REQUIRED_MEMBERS - members)
    if missing:
        raise RuntimeError(f"Wheel is missing runtime resources: {', '.join(missing)}")
    if not any(name.endswith(".dist-info/entry_points.txt") for name in members):
        raise RuntimeError("Wheel is missing console entry-point metadata")
    if not any(".dist-info/licenses/LICENSE" in name for name in members):
        raise RuntimeError("Wheel is missing Apache license material")

    with tempfile.TemporaryDirectory(prefix="room-alignment-package-") as temporary:
        root = Path(temporary)
        environment_dir = root / "environment"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_dir)
        scripts = environment_dir / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        command = scripts / ("room-alignment.exe" if os.name == "nt" else "room-alignment")
        clean_environment = os.environ.copy()
        clean_environment.pop("PYTHONPATH", None)
        clean_environment["PYTHONUNBUFFERED"] = "1"
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            cwd=root,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        version = subprocess.run(
            [str(command), "--version"],
            cwd=root,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        module_version = subprocess.run(
            [str(python), "-m", "room_alignment", "--version"],
            cwd=root,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if module_version != version:
            raise RuntimeError("Console and module entry points report different versions")
        expected_system_version = version.removeprefix("room-alignment ")
        if expected_system_version == version:
            raise RuntimeError("Installed console version has an unexpected format")
        doctor = subprocess.run(
            [str(command), "doctor"],
            cwd=root,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        )
        doctor_payload = json.loads(doctor.stdout)
        state_dir = root / "state"

        process = _launch(command, state_dir, root, clean_environment)
        try:
            launch_url = _read_launch_url(process)
            base = launch_url.split("/bootstrap/", 1)[0]
            cookies = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
            index = opener.open(launch_url, timeout=5).read().decode("utf-8")
            health = json.load(opener.open(f"{base}/api/health", timeout=5))
            system = json.load(opener.open(f"{base}/api/v1/system", timeout=5))
            openapi = json.load(opener.open(f"{base}/api/v1/openapi.json", timeout=5))
            if "Room Alignment" not in index:
                raise RuntimeError("Installed frontend did not load")
            if not health.get("ok") or system.get("version") != expected_system_version:
                raise RuntimeError("Installed health/system version mismatch")
            if openapi.get("openapi") != "3.1.0":
                raise RuntimeError("Installed OpenAPI contract did not load")
        finally:
            if process.poll() is None:
                try:
                    _stop_via_cli(command, state_dir, root, clean_environment, process)
                finally:
                    _stop(process)

        administration = subprocess.run(
            [str(command), "admin", "verify", str(state_dir / "room-alignment.sqlite3")],
            cwd=root,
            env=clean_environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if "integrity=ok" not in administration:
            raise RuntimeError("Installed state-administration command failed integrity verification")

        # Reuse the same state directory after SIGTERM to prove lock release.
        second = _launch(command, state_dir, root, clean_environment)
        try:
            _read_launch_url(second)
        finally:
            _stop(second)

    return {
        "wheel": wheel.name,
        "wheelSha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "version": version,
        "doctorReady": bool(doctor_payload.get("ready")),
        "frontend": "loaded",
        "health": "ok",
        "openapi": "3.1.0",
        "sigterm": "clean",
        "stopCommand": "ok",
        "stateLockReuse": "ok",
        "stateAdmin": "ok",
        "sourceTreeIndependent": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an installable Room Alignment wheel")
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(verify(args.wheel), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
