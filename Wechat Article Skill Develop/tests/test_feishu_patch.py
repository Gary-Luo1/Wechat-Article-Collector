"""Partial Feishu patch merge semantics and grant error classification."""

from __future__ import annotations

import io
import json


def _configured(monkeypatch, home) -> None:
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(home))
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["wechat"] = {"cookie": "cookie-secret", "token": "token-secret"}
    config["subscriptions"] = [{"name": "Example"}]
    config["setup"]["feishu_identity_confirmed"] = True
    config["setup"]["feishu_authorization"] = {
        "state": "authorized",
        "identity": "user",
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "unlisted_publisher": "ask",
            "allow_feishu_sync": True,
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "identity": "user",
            "binding_mode": "existing",
            "base_token": "bas_abc",
            "table_id": "tbl_abc",
            "field_mapping": {
                "title": {"field_id": "fld_title", "name": "标题", "type": "text"}
            },
        }
    )
    save_config(config)


def _patch_feishu(monkeypatch, home, payload: dict) -> int:
    import init_config

    monkeypatch.setattr(
        init_config.sys, "stdin", io.StringIO(json.dumps(payload))
    )
    return init_config.main(
        ["--agent-stdin", "--section", "feishu", "--format", "json"]
    )


def test_partial_feishu_patch_preserves_policy_and_omitted_fields(
    tmp_path, monkeypatch, capsys
):
    from config_store import load_config

    _configured(monkeypatch, tmp_path / "state")
    assert (
        _patch_feishu(
            monkeypatch,
            tmp_path / "state",
            {
                "destination": "existing",
                "enabled": True,
                "base_token": "bas_abc",
                "table_id": "tbl_abc",
            },
        )
        == 0
    )
    capsys.readouterr()
    saved = load_config()
    assert saved["setup"]["execution_policy"]["confirmed"] is True
    assert saved["feishu"]["binding_mode"] == "existing"
    assert saved["feishu"]["field_mapping"]["title"]["field_id"] == "fld_title"


def test_feishu_patch_explicit_scope_change_still_invalidates(
    tmp_path, monkeypatch, capsys
):
    from config_store import load_config

    _configured(monkeypatch, tmp_path / "state")
    assert (
        _patch_feishu(
            monkeypatch,
            tmp_path / "state",
            {
                "destination": "existing",
                "enabled": True,
                "base_token": "bas_abc",
                "table_id": "tbl_abc",
                "binding_mode": "dedicated",
            },
        )
        == 0
    )
    capsys.readouterr()
    saved = load_config()
    assert saved["setup"]["execution_policy"]["confirmed"] is False
    assert saved["feishu"]["binding_mode"] == "dedicated"


def test_feishu_patch_preserves_destination_when_untouched(
    tmp_path, monkeypatch, capsys
):
    from config_store import DEFAULT_CONFIG, load_config, save_config

    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["wechat"] = {"cookie": "cookie-secret", "token": "token-secret"}
    config["subscriptions"] = [{"name": "Example"}]
    config["feishu"].update(
        {
            "destination": "create",
            "enabled": False,
            "identity": "user",
            "binding_mode": "existing",
        }
    )
    save_config(config)

    assert _patch_feishu(monkeypatch, tmp_path / "state", {"schema_policy": "mapped"}) == 0
    capsys.readouterr()
    saved = load_config()
    assert saved["feishu"]["destination"] == "create"


def test_feishu_patch_derives_destination_and_enablement_from_new_target(
    tmp_path, monkeypatch, capsys
):
    from config_store import DEFAULT_CONFIG, load_config, save_config

    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["wechat"] = {"cookie": "cookie-secret", "token": "token-secret"}
    config["subscriptions"] = [{"name": "Example"}]
    config["feishu"].update(
        {
            "destination": "create",
            "enabled": False,
            "identity": "user",
        }
    )
    save_config(config)

    assert (
        _patch_feishu(
            monkeypatch,
            tmp_path / "state",
            {"base_token": "bas_new", "table_id": "tbl_new"},
        )
        == 0
    )
    capsys.readouterr()
    saved = load_config()
    assert saved["feishu"]["destination"] == "existing"
    assert saved["feishu"]["enabled"] is True


def test_lark_duplicate_member_error_is_classified(tmp_path):
    from bitable_client import _payload_error

    duplicate = _payload_error(
        {
            "error": {
                "code": "160002",
                "type": "api",
                "message": "member already exists",
            }
        },
        [],
    )
    assert duplicate.kind == "duplicate"

    permission = _payload_error(
        {"error": {"code": "91403", "type": "api", "message": "no permission"}}, []
    )
    assert permission.kind == "permission"
