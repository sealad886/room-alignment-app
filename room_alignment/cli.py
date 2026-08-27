from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .lifecycle import stop
from .server import CONTRACT, CONTRACTS, WEB, add_serve_arguments, serve
from .state_admin import add_admin_arguments, run_admin


TOOL_VERSION_PATTERN = re.compile(r"\bversion\s+n?(\d+)(?:\.(\d+))?", re.IGNORECASE)
MINIMUM_MEDIA_TOOL_MAJOR = 6


def _tool_status(name: str) -> dict[str, object]:
    executable = shutil.which(name)
    if not executable:
        return {"available": False, "supported": False, "version": None}
    try:
        result = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "supported": False, "version": None}
    first_line = (result.stdout or result.stderr).splitlines()
    version_line = first_line[0][:200] if first_line else None
    match = TOOL_VERSION_PATTERN.search(version_line or "")
    supported = bool(
        result.returncode == 0
        and match
        and int(match.group(1)) >= MINIMUM_MEDIA_TOOL_MAJOR
    )
    return {
        "available": result.returncode == 0,
        "supported": supported,
        "version": version_line,
    }


def doctor() -> int:
    resources = {
        "frontend": (WEB / "index.html").is_file(),
        "openapi": CONTRACT.is_file(),
        "schemas": all(
            (CONTRACTS / name).is_file()
            for name in (
                "api.schema.json",
                "commands.schema.json",
                "domain.schema.json",
                "manifest.schema.json",
            )
        ),
    }
    tools = {name: _tool_status(name) for name in ("ffmpeg", "ffprobe")}
    ready = all(resources.values()) and all(bool(value["supported"]) for value in tools.values())
    print(
        json.dumps(
            {
                "application": "room-alignment",
                "version": __version__,
                "ready": ready,
                "python": sys.version.split()[0],
                "resources": resources,
                "tools": tools,
            },
            sort_keys=True,
        )
    )
    return 0 if ready else 1


def parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser for the application.
    
    Returns:
    	argparse.ArgumentParser: The parser configured with application commands and options.
    """
    root = argparse.ArgumentParser(
        prog="room-alignment",
        description="Installable local-first multi-camera video alignment application",
    )
    root.add_argument("--version", action="version", version=f"room-alignment {__version__}")
    commands = root.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="start local application and open secure browser session")
    add_serve_arguments(serve_parser)
    commands.add_parser("doctor", help="check packaged resources and external media tools")
    stop_parser = commands.add_parser("stop", help="stop the application owning a state directory")
    stop_parser.add_argument("--data-dir", type=Path, default=Path.home() / ".room-alignment")
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="seconds to wait for graceful shutdown (default: 10)",
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="send SIGKILL if graceful shutdown exceeds the timeout",
    )
    admin_parser = commands.add_parser("admin", help="verify, back up, migrate-check, or restore state")
    add_admin_arguments(admin_parser)
    return root


def _normalized_argv(argv: list[str]) -> list[str]:
    """
    Normalize command-line arguments by selecting the default command when needed.
    
    Parameters:
        argv (list[str]): Command-line arguments to normalize.
    
    Returns:
        list[str]: Arguments with ``serve`` inserted when no command is specified.
    """
    if not argv:
        return ["serve"]
    if argv[0] in {"serve", "stop", "doctor", "admin", "--help", "-h", "--version"}:
        return argv
    # Preserve the original `room-alignment --host ...` launch shape.
    return ["serve", *argv]


def main(argv: list[str] | None = None) -> int:
    """
    Run the command-line interface and dispatch the selected command.
    
    Parameters:
        argv (list[str] | None): Command-line arguments to process, or None to use the process arguments.
    
    Returns:
        int: Exit status, with 1 when the stop command times out and 0 otherwise.
    """
    arguments = _normalized_argv(list(sys.argv[1:] if argv is None else argv))
    args = parser().parse_args(arguments)
    if args.command == "serve":
        return serve(args)
    if args.command == "doctor":
        return doctor()
    if args.command == "stop":
        result = stop(args.data_dir, args.timeout, args.force)
        print(json.dumps(result, sort_keys=True))
        return 1 if result["status"] == "TIMEOUT" else 0
    if args.command == "admin":
        return run_admin(args)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
