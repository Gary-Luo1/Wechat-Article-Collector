"""Field-mapping invalidation behavior for the Feishu check save path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def configured(home: Path) -> dict:
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["wechat"] = {"cookie": "cookie-secret", "token": "token-secret"}
    config["subscriptions"] = [{"name": "Example"}]
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "identity": "bot",
            "expected_app_id": "cli_abc",
            "cli_profile": "skill-cli_abc",
            "base_token": "base_token",
            "table_id": "table_id",
        }
    )
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "allow_feishu_sync": True,
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    save_config(config)
    return config


def _run_check(monkeypatch: pytest.MonkeyPatch, mapping: dict) -> int:
    import process_pending

    monkeypatch.setattr(
        process_pending,
        "lark_cli_info",
        lambda: {"version": "1.0.69", "compatible": True},
    )
    monkeypatch.setattr(
        process_pending,
        "preflight_feishu",
        lambda feishu: {
            "identity": "bot",
            "field_count": len(mapping),
            "mapping": mapping,
        },
    )
    return process_pending.cmd_feishu_check(save_mapping=True)


MAPPING_A = {
    "title": {"field_id": "fld_title", "name": "Title", "type": "text"},
    "url": {"field_id": "fld_url", "name": "URL", "type": "url"},
}
MAPPING_B = {
    "title": {"field_id": "fld_other", "name": "Other", "type": "text"},
    "url": {"field_id": "fld_url", "name": "URL", "type": "url"},
}


def test_saving_changed_mapping_invalidates_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    from config_store import load_config, save_config

    configured(tmp_path / "state")
    assert _run_check(monkeypatch, MAPPING_A) == 0
    assert load_config()["feishu"]["field_mapping"] == MAPPING_A
    assert load_config()["setup"]["execution_policy"]["confirmed"] is False

    # Saving the identical mapping again must not invalidate a fresh approval.
    config = load_config()
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "allow_feishu_sync": True,
            "approved_at": "2026-01-02T00:00:00+00:00",
        }
    )
    save_config(config)
    assert _run_check(monkeypatch, MAPPING_A) == 0
    assert load_config()["setup"]["execution_policy"]["confirmed"] is True

    # A different mapping changes the authorized scope and must invalidate.
    assert _run_check(monkeypatch, MAPPING_B) == 0
    saved = load_config()
    assert saved["feishu"]["field_mapping"] == MAPPING_B
    assert saved["setup"]["execution_policy"]["confirmed"] is False
    capsys.readouterr()
