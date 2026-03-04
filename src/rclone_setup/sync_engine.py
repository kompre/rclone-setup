from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

from rclone_setup.config import SyncPair

REMOTE_NAME = "tecnico"
BISYNC_TIMEOUT = 300


@dataclass
class SyncResult:
    success: bool
    output: str
    error: str
    timestamp: str


def test_connection() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["rclone", "lsd", f"{REMOTE_NAME}:{REMOTE_NAME}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, "Connection OK"
        return False, result.stderr.strip() or "Unknown error"
    except FileNotFoundError:
        return False, "rclone not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "Connection timed out"


def normalize_remote_path(raw: str) -> str:
    """Convert a pasted Windows/UNC path to a valid rclone remote subpath."""
    path = raw.replace("\\", "/").strip().strip("/")
    # Strip UNC prefix like //server/share/ or //server/tecnico/
    if path.startswith("//"):
        parts = path.split("/")
        # //server/share/rest → drop first 4 empty+server+share, keep rest
        if len(parts) > 3:
            path = "/".join(parts[4:])
        else:
            path = ""
    return path


def list_remote_dirs(subpath: str = "") -> tuple[bool, list[str]]:
    """List directories at a remote subpath. Returns (ok, dir_names)."""
    target = f"{REMOTE_NAME}:{REMOTE_NAME}"
    if subpath:
        target += f"/{subpath}"
    try:
        result = subprocess.run(
            ["rclone", "lsd", target],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, []
        dirs = []
        for line in result.stdout.splitlines():
            # lsd output: "          -1 2024-01-01 00:00:00        -1 dirname"
            parts = line.split(None, 4)
            if len(parts) >= 5:
                dirs.append(parts[4])
        dirs.sort()
        return True, dirs
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, []


def ensure_local_path(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def run_bisync(pair: SyncPair, resync: bool = False) -> SyncResult:
    remote = f"{REMOTE_NAME}:{pair.remote_path}"
    local = pair.local_path

    cmd = ["rclone", "bisync", remote, local, "--verbose"]
    if resync:
        cmd.append("--resync")
    else:
        cmd.extend(["--resilient", "--recover"])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=BISYNC_TIMEOUT,
        )
        return SyncResult(
            success=result.returncode == 0,
            output=result.stdout,
            error=result.stderr,
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )
    except subprocess.TimeoutExpired:
        return SyncResult(
            success=False,
            output="",
            error="Bisync timed out after 300s",
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )
    except FileNotFoundError:
        return SyncResult(
            success=False,
            output="",
            error="rclone not found in PATH",
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )
