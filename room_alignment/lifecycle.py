from __future__ import annotations

import fcntl
import json
import os
import signal
import time
from pathlib import Path
from typing import TextIO


LOCK_NAME = "application.lock"
LOCK_SCHEMA_VERSION = 1


def write_owner(lock_file: TextIO) -> None:
    """Publish process identity only after this process owns the state lock."""

    payload = {
        "application": "room-alignment",
        "pid": os.getpid(),
        "schemaVersion": LOCK_SCHEMA_VERSION,
    }
    lock_file.seek(0)
    lock_file.truncate()
    json.dump(payload, lock_file, sort_keys=True)
    lock_file.write("\n")
    lock_file.flush()
    os.fsync(lock_file.fileno())


def clear_owner(lock_file: TextIO) -> None:
    """Clear process identity while caller still owns the state lock."""

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _owner_pid(lock_file: TextIO) -> int:
    """Read and validate the process identity stored in a state lock file.
    
    Parameters:
    	lock_file (TextIO): The lock file containing process ownership metadata.
    
    Returns:
    	int: The owning process ID.
    
    Raises:
    	ValueError: If the lock file contains invalid, malformed, or unrelated ownership data.
    """
    lock_file.seek(0)
    try:
        payload = json.load(lock_file)
    except (json.JSONDecodeError, OSError, TypeError) as error:
        raise ValueError("State lock has no valid Room Alignment process identity") from error
    if not isinstance(payload, dict) or payload.get("application") != "room-alignment":
        raise ValueError("State lock is not owned by Room Alignment")
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ValueError("State lock has no valid Room Alignment process identity")
    return pid


def _try_lock(lock_file: TextIO) -> bool:
    """Attempt to acquire an exclusive nonblocking lock on the file.
    
    Returns:
    	bool: `true` if the lock was acquired, `false` if another process holds it.
    """
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def stop(data_dir: Path, timeout_seconds: float = 10.0, force: bool = False) -> dict[str, object]:
    """Stop the process owning a Room Alignment state directory.
    
    Parameters:
    	data_dir (Path): State directory containing the application lock file.
    	timeout_seconds (float): Maximum time to wait for graceful termination.
    	force (bool): Whether to force termination after the graceful timeout.
    
    Returns:
    	dict[str, object]: Status information with `status` set to `NOT_RUNNING`, `STOPPED`, or `TIMEOUT`, and `forced` indicating whether forced termination was used.
    
    Raises:
    	ValueError: If `timeout_seconds` is negative.
    	RuntimeError: If stopping fails due to permissions or the lock owner changes.
    """

    if timeout_seconds < 0:
        raise ValueError("Stop timeout must be zero or greater")
    lock_path = data_dir.expanduser().resolve() / LOCK_NAME
    if not lock_path.exists():
        return {"status": "NOT_RUNNING", "forced": False}

    with lock_path.open("r+") as lock_file:
        if _try_lock(lock_file):
            try:
                return {"status": "NOT_RUNNING", "forced": False}
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        pid = _owner_pid(lock_file)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            # Ownership can disappear between the lock check and signal delivery.
            if _try_lock(lock_file):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                return {"status": "NOT_RUNNING", "forced": False}
            raise RuntimeError("Room Alignment owner changed while stopping") from None
        except PermissionError as error:
            raise RuntimeError("Permission denied while stopping Room Alignment") from error

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if _try_lock(lock_file):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                return {"status": "STOPPED", "forced": False}
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

        if _try_lock(lock_file):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return {"status": "STOPPED", "forced": False}
        if not force:
            return {"status": "TIMEOUT", "forced": False}

        current_pid = _owner_pid(lock_file)
        try:
            os.kill(current_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise RuntimeError("Permission denied while force-stopping Room Alignment") from error

        force_deadline = time.monotonic() + 5.0
        while time.monotonic() < force_deadline:
            if _try_lock(lock_file):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                return {"status": "STOPPED", "forced": True}
            time.sleep(0.1)
        return {"status": "TIMEOUT", "forced": True}
