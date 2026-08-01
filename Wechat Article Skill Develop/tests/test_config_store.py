"""Configuration transaction and concurrency tests for the config store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "wechat-article-subscriber" / "scripts"


def configured(home: Path) -> dict:
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "allow_feishu_provisioning": False,
            "allow_feishu_sync": True,
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "identity": "user",
            "expected_app_id": "cli_abc",
            "expected_user_open_id": "ou_user",
            "base_token": "bas_abc",
            "table_id": "tbl_abc",
        }
    )
    save_config(config)
    return config


def test_stale_mutator_does_not_resurrect_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    from config_store import load_config, modify_config
    from execution_policy import invalidate_policy

    configured(tmp_path / "state")
    stale = load_config()
    assert stale["setup"]["execution_policy"]["confirmed"] is True

    modify_config(invalidate_policy)
    assert load_config()["setup"]["execution_policy"]["confirmed"] is False

    # A second writer commits from its own stale snapshot; the transaction must
    # reload the latest config instead of resurrecting the old approval.
    def stale_write(config):
        assert config["setup"]["execution_policy"]["confirmed"] is False
        config["health"]["feishu"]["last_verified_at"] = "2026-01-02T00:00:00+00:00"
        return config

    modify_config(stale_write)
    final = load_config()
    assert final["setup"]["execution_policy"]["confirmed"] is False
    assert final["health"]["feishu"]["last_verified_at"] == "2026-01-02T00:00:00+00:00"


def test_update_health_does_not_resurrect_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    from config_store import load_config, modify_config, update_health
    from execution_policy import invalidate_policy

    configured(tmp_path / "state")
    modify_config(invalidate_policy)

    update_health("feishu", success=True)

    final = load_config()
    assert final["setup"]["execution_policy"]["confirmed"] is False
    assert final["health"]["feishu"]["last_failure_kind"] == ""
    assert final["health"]["feishu"]["consecutive_failures"] == 0


def test_modify_config_validation_failure_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    from config_store import ConfigError, load_config, modify_config

    configured(tmp_path / "state")
    before = load_config()

    def corrupt(config):
        config["setup"]["feishu_authorization"]["state"] = "bogus"

    with pytest.raises(ConfigError):
        modify_config(corrupt)
    assert load_config() == before


def test_config_lock_serializes_cross_process_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    home = tmp_path / "state"
    configured(home)

    bump = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.environ['WAS_SCRIPTS'])\n"
        "from config_store import modify_config\n"
        "for _ in range(5):\n"
        "    modify_config(lambda c: _bump(c))\n"
    )
    script = (
        "def _bump(c):\n"
        "    c['health']['subscriptions']['unresolved'] = "
        "int(c['health']['subscriptions']['unresolved']) + 1\n"
        "    return c\n"
        + bump
    )
    env = dict(os.environ)
    env["WECHAT_ARTICLE_HOME"] = str(home)
    env["WAS_SCRIPTS"] = str(SCRIPTS)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for proc in processes:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr

    from config_store import load_config

    assert load_config()["health"]["subscriptions"]["unresolved"] == 10
