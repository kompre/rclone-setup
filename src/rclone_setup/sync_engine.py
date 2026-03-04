from __future__ import annotations

import configparser
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from rclone_setup.config import APP_CACHE_DIR, APP_RCLONE_CONFIG, SyncPair


def _base_args() -> list[str]:
    return ["--config", APP_RCLONE_CONFIG, "--cache-dir", APP_CACHE_DIR]


def ensure_rclone_config() -> tuple[bool, str]:
    """Copy system rclone config to app dir on first run. Returns (ok, message)."""
    if os.path.exists(APP_RCLONE_CONFIG):
        return True, "App rclone config already exists."

    os.makedirs(os.path.dirname(APP_RCLONE_CONFIG), exist_ok=True)
    os.makedirs(APP_CACHE_DIR, exist_ok=True)

    try:
        result = subprocess.run(
            ["rclone", "config", "file"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, "rclone not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "rclone config file timed out"

    # Output is like: "Configuration file is stored at:\nC:\Users\...\rclone.conf\n"
    system_config = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and not line.lower().startswith("configuration"):
            system_config = line
            break

    if not system_config or not os.path.exists(system_config):
        return False, f"System rclone config not found (got: {system_config!r})"

    shutil.copy2(system_config, APP_RCLONE_CONFIG)
    return True, f"Copied rclone config from {system_config}"


@dataclass
class SyncResult:
    success: bool
    output: str
    error: str
    timestamp: str


def _is_rclone_remote_path(path: str) -> bool:
    """Return True if path is an rclone remote path (colon with prefix longer than 1 char)."""
    if ":" not in path:
        return False
    prefix = path.split(":", 1)[0]
    return len(prefix) > 1


def list_remotes() -> list[str]:
    """Run rclone listremotes, return names without trailing ':'."""
    try:
        result = subprocess.run(
            ["rclone", *_base_args(), "listremotes"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return []
        return [line.strip().rstrip(":") for line in result.stdout.splitlines() if line.strip()]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _get_smb_remotes() -> dict[str, tuple[str, str]]:
    """Parse rclone config INI to find SMB remotes. Returns {remote_name: (host, share)}."""
    if not os.path.exists(APP_RCLONE_CONFIG):
        return {}
    parser = configparser.ConfigParser()
    parser.read(APP_RCLONE_CONFIG, encoding="utf-8")
    remotes: dict[str, tuple[str, str]] = {}
    for section in parser.sections():
        cfg = dict(parser[section])
        if cfg.get("type") in ("smb", "cifs"):
            host = cfg.get("host", "").lower()
            share = cfg.get("share", "").strip("/").lower()
            if host:
                remotes[section] = (host, share)
    return remotes


def match_unc_to_remote(path: str) -> str | None:
    """Match a UNC path like \\\\server\\share\\sub to a known SMB remote.
    Returns 'remote:sub' string or None."""
    norm = path.replace("\\", "/").strip()
    if not norm.startswith("//"):
        return None
    parts = [p for p in norm.split("/") if p]
    if len(parts) < 2:
        return None
    server = parts[0].lower()
    share = parts[1].lower()
    subpath = "/".join(parts[2:])
    for remote_name, (host, remote_share) in _get_smb_remotes().items():
        if host == server and remote_share == share:
            return f"{remote_name}:{subpath}" if subpath else f"{remote_name}:"
    return None


def _get_unc_for_drive(drive_letter: str) -> str | None:
    """Return the UNC path (e.g. \\\\server\\share) for a mapped Windows drive letter,
    or None if the drive is not a mapped network drive."""
    try:
        import ctypes
        mpr = ctypes.WinDLL("mpr")
        buf = ctypes.create_unicode_buffer(1024)
        buf_size = ctypes.c_ulong(1024)
        dl = drive_letter.upper().rstrip(":\\/") + ":"
        ret = mpr.WNetGetConnectionW(dl, buf, ctypes.byref(buf_size))
        if ret == 0:
            return buf.value
        return None
    except Exception:
        return None


def match_mapped_drive_to_remote(path: str) -> str | None:
    """Try to match a mapped drive path like T:\\sub\\dir to an rclone remote path.
    Resolves the drive letter to its UNC path then delegates to match_unc_to_remote."""
    norm = path.replace("\\", "/").strip()
    # Must start with a single drive letter followed by ':'
    if len(norm) < 2 or not norm[0].isalpha() or norm[1] != ":":
        return None
    # After "X:" must be nothing, or "/"
    if len(norm) > 2 and norm[2] != "/":
        return None
    drive_letter = norm[0].upper() + ":"
    subpath = norm[3:] if len(norm) > 3 else ""
    unc = _get_unc_for_drive(drive_letter)
    if unc is None:
        return None
    # Build full UNC path including subpath, then use the existing matcher
    full_unc = unc.rstrip("\\")
    if subpath:
        full_unc = full_unc + "\\" + subpath.replace("/", "\\")
    return match_unc_to_remote(full_unc)


def normalize_path(raw: str) -> str:
    """Normalize a path: rclone remote paths pass through unchanged;
    UNC paths and mapped drive paths matching an SMB remote are converted;
    everything else is returned as-is."""
    stripped = raw.strip()
    if _is_rclone_remote_path(stripped):
        return stripped
    matched = match_unc_to_remote(stripped)
    if matched is not None:
        return matched
    matched = match_mapped_drive_to_remote(stripped)
    if matched is not None:
        return matched
    return stripped


def list_remote_dirs(path: str) -> tuple[bool, list[str]]:
    """List directories at a full rclone path (e.g. 'remote:' or 'remote:subdir').
    Returns (ok, dir_names)."""
    try:
        result = subprocess.run(
            ["rclone", *_base_args(), "lsd", path],
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
    if not _is_rclone_remote_path(path):
        os.makedirs(path, exist_ok=True)


def test_connection(paths: list[str] | None = None) -> tuple[bool, str]:
    """Test connectivity. Checks each unique remote prefix found in paths.
    If no paths given (or no remotes in paths), just verifies rclone is available."""
    try:
        remote_prefixes: list[str] = []
        if paths:
            for p in paths:
                if _is_rclone_remote_path(p):
                    prefix = p.split(":", 1)[0]
                    if prefix not in remote_prefixes:
                        remote_prefixes.append(prefix)

        if not remote_prefixes:
            subprocess.run(["rclone", "version"], capture_output=True, timeout=10, check=True)
            return True, "rclone available"

        for prefix in remote_prefixes:
            result = subprocess.run(
                ["rclone", *_base_args(), "lsd", f"{prefix}:"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False, f"Remote '{prefix}': {result.stderr.strip() or 'Unknown error'}"
        return True, "Connection OK"
    except FileNotFoundError:
        return False, "rclone not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "Connection timed out"
    except subprocess.CalledProcessError:
        return False, "rclone check failed"


def run_bisync(
    pair: SyncPair,
    resync: bool = False,
    extra_flags: list[str] | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> SyncResult:
    """Run rclone bisync with no timeout, streaming output line-by-line via log_callback.
    stderr is merged into stdout so all verbose output is captured together."""
    cmd = ["rclone", *_base_args(), "bisync", pair.path1, pair.path2, "--verbose"]
    if resync:
        cmd.append("--resync")
    else:
        cmd.extend(["--resilient", "--recover"])
    if extra_flags:
        cmd.extend(extra_flags)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr so verbose lines stream in order
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        lines: list[str] = []
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip("\n")
            lines.append(line)
            if log_callback and line:
                log_callback(line)
        proc.wait()
        output = "\n".join(lines)
        return SyncResult(
            success=proc.returncode == 0,
            output=output,
            error="",
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )
    except FileNotFoundError:
        return SyncResult(
            success=False,
            output="",
            error="rclone not found in PATH",
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )


def open_rclone_config() -> None:
    """Open an interactive rclone config terminal in a new console window."""
    subprocess.Popen(
        ["cmd.exe", "/c", "rclone", "--config", APP_RCLONE_CONFIG, "config"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
