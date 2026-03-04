from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "rclone-setup")
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DEFAULT_LOG_PATH = os.path.join(APP_DIR, "last_run.log")
APP_RCLONE_CONFIG = os.path.join(APP_DIR, "rclone.conf")
APP_CACHE_DIR = os.path.join(APP_DIR, "cache")


@dataclass
class SyncPair:
    path1: str
    path2: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    initialized: bool = False
    enabled: bool = False  # whether to include in scheduled sync runs


def _pair_from_dict(d: dict) -> SyncPair:
    # Migration: old keys (remote_path / local_path) -> new keys (path1 / path2)
    d = dict(d)
    if "remote_path" in d and "path1" not in d:
        d["path1"] = d.pop("remote_path")
    if "local_path" in d and "path2" not in d:
        d["path2"] = d.pop("local_path")
    return SyncPair(**d)


@dataclass
class AppConfig:
    sync_interval_minutes: int = 15
    bisync_flags: list[str] = field(default_factory=list)
    pairs: list[SyncPair] = field(default_factory=list)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not os.path.exists(path):
        return AppConfig()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pairs = [_pair_from_dict(p) for p in data.get("pairs", [])]
    return AppConfig(
        sync_interval_minutes=data.get("sync_interval_minutes", 15),
        bisync_flags=data.get("bisync_flags", []),
        pairs=pairs,
    )


def save_config(config: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    data = {
        "sync_interval_minutes": config.sync_interval_minutes,
        "bisync_flags": config.bisync_flags,
        "pairs": [asdict(p) for p in config.pairs],
    }
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
