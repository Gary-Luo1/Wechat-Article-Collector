"""Platform-aware paths and secure JSON persistence."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


APP_NAME = "wechat-article-subscriber"


def data_dir() -> Path:
    """Return the writable per-user data directory.

    WECHAT_ARTICLE_HOME is intentionally supported for tests, portable installs,
    and users who do not want state in the platform default location.
    """
    override = os.environ.get("WECHAT_ARTICLE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / APP_NAME
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / ".local" / "state" / APP_NAME


def sys_platform() -> str:
    import sys

    return sys.platform


def config_path() -> Path:
    return data_dir() / "config.json"


def queue_path() -> Path:
    return data_dir() / "queue.json"


def lock_path() -> Path:
    return data_dir() / "queue.lock"


def venv_dir() -> Path:
    return data_dir() / "venv"


def secure_write_json(path: Path, value: Any) -> None:
    """Atomically write JSON and restrict permissions where supported."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if os.name != "nt":
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise
