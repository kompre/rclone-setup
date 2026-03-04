from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "rclone-setup")
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DEFAULT_LOG_PATH = os.path.join(APP_DIR, "last_run.log")


@dataclass
class SyncPair:
    remote_path: str
    local_path: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    initialized: bool = False
    enabled: bool = True


@dataclass
class AppConfig:
    sync_interval_minutes: int = 15
    pairs: list[SyncPair] = field(default_factory=list)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not os.path.exists(path):
        return AppConfig()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pairs = [SyncPair(**p) for p in data.get("pairs", [])]
    return AppConfig(
        sync_interval_minutes=data.get("sync_interval_minutes", 15),
        pairs=pairs,
    )


def save_config(config: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    data = {
        "sync_interval_minutes": config.sync_interval_minutes,
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
