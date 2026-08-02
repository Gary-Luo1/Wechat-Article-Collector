"""Runtime path-resolution tests for the single state-directory rule."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_runtime_venv_derives_from_paths_module(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))

    import runtime
    from paths import venv_dir

    expected = venv_dir() / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert runtime._venv_python() == expected


@pytest.mark.skipif(os.name == "nt", reason="XDG fallback is POSIX behavior")
def test_empty_xdg_state_home_falls_back_like_paths(tmp_path, monkeypatch):
    import runtime
    from paths import APP_NAME, data_dir

    monkeypatch.delenv("WECHAT_ARTICLE_HOME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setattr("paths.sys_platform", lambda: "linux")

    expected = Path.home() / ".local" / "state" / APP_NAME
    assert data_dir() == expected
    assert runtime._venv_python() == expected / "venv" / "bin/python"
