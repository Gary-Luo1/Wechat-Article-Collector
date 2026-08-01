#!/usr/bin/env python3
"""Run skill commands through the isolated virtual environment when installed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


COMMANDS = {
    "setup": "init_config.py",
    "discover": "discover_only.py",
    "process": "process_pending.py",
    "manage": "manage.py",
    "lark": "lark_cli.py",
}


def _data_dir() -> Path:
    override = os.environ.get("WECHAT_ARTICLE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / "wechat-article-subscriber"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "wechat-article-subscriber"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "wechat-article-subscriber"


def _venv_python() -> Path:
    root = _data_dir() / "venv"
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _system_runtime_is_ready(command: str) -> bool:
    if command in {"setup", "manage", "lark", "process"}:
        return True
    try:
        __import__("requests")
        __import__("bs4")
    except ImportError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] not in COMMANDS:
        print("usage: runtime.py {setup|discover|process|manage|lark} [args...]", file=sys.stderr)
        return 2
    command = args.pop(0)
    script = Path(__file__).resolve().parent / COMMANDS[command]
    interpreter = _venv_python()
    if not interpreter.exists() and _system_runtime_is_ready(command):
        interpreter = Path(sys.executable)
    if not interpreter.exists():
        print(
            "Python dependencies are unavailable; run the repository installer first",
            file=sys.stderr,
        )
        return 1
    result = subprocess.run(
        [str(interpreter), str(script), *args],
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
