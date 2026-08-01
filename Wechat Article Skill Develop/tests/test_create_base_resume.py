"""Base-creation resume and foreign-target guard tests."""

from __future__ import annotations

import json

import pytest


def _configured(monkeypatch: pytest.MonkeyPatch, home) -> None:
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(home))
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["wechat"] = {"cookie": "cookie-secret", "token": "token-secret"}
    config["subscriptions"] = [{"name": "Example"}]
    config["setup"]["feishu_identity_confirmed"] = True
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "unlisted_publisher": "ask",
            "allow_feishu_provisioning": True,
            "provision_base_name": "公众号文章",
            "provision_table_name": "文章列表",
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "expected_app_id": "cli_example123",
            "cli_profile": "wechat-article-profile",
            "manager_open_id": "ou_manager",
        }
    )
    save_config(config)


def _created_payload():
    return {
        "ok": True,
        "data": {
            "base": {"app_token": "bascn_created"},
            "created_table_id": "tbl_created",
        },
    }


def test_feishu_create_base_resumes_after_grant_failure(tmp_path, monkeypatch, capsys):
    import manage
    from bitable_client import LarkCLIError
    from config_store import load_config

    _configured(monkeypatch, tmp_path / "state")
    calls = {"create": 0, "grant": 0}
    monkeypatch.setattr(manage, "verify_feishu_identity", lambda *a, **k: {"status": "ready"})

    def create_standard_base(*args, **kwargs):
        calls["create"] += 1
        return _created_payload()

    monkeypatch.setattr(manage, "create_standard_base", create_standard_base)

    def failing_grant(*args, **kwargs):
        calls["grant"] += 1
        raise LarkCLIError("member already exists", kind="duplicate")

    monkeypatch.setattr(manage, "grant_bot_created_resource", failing_grant)
    monkeypatch.setattr(
        manage,
        "preflight_feishu",
        lambda *a, **k: {
            "mapping": {
                "title": {"field_id": "fld_title", "name": "文章标题", "type": "text"},
                "url": {"field_id": "fld_url", "name": "文章链接", "type": "url"},
            }
        },
    )

    assert (
        manage.main(
            ["feishu-create-base", "--name", "公众号文章", "--table-name", "文章列表", "--yes"]
        )
        == 1
    )
    capsys.readouterr()
    saved = load_config()["feishu"]
    assert saved["provisioning"] == "created"
    assert saved["enabled"] is False
    assert saved["base_token"] == "bascn_created"
    assert saved["table_id"] == "tbl_created"
    assert saved["created_base_name"] == "公众号文章"
    assert saved["created_table_name"] == "文章列表"

    # Retry: the Base must not be created twice; grant now succeeds.
    monkeypatch.setattr(
        manage,
        "grant_bot_created_resource",
        lambda *a, **k: {"ok": True},
    )
    assert (
        manage.main(
            ["feishu-create-base", "--name", "公众号文章", "--table-name", "文章列表", "--yes"]
        )
        == 0
    )
    capsys.readouterr()
    final = load_config()
    assert calls["create"] == 1
    assert final["feishu"]["enabled"] is True
    assert final["feishu"]["field_mapping"]["title"]["field_id"] == "fld_title"
    assert final["setup"]["execution_policy"]["allow_feishu_provisioning"] is False
    assert final["health"]["feishu"]["last_verified_at"]


def test_feishu_create_base_resume_rejects_non_duplicate_grant_failure(
    tmp_path, monkeypatch, capsys
):
    import manage
    from bitable_client import LarkCLIError
    from config_store import load_config, save_config

    _configured(monkeypatch, tmp_path / "state")
    config = load_config()
    config["feishu"].update(
        {
            "provisioning": "created",
            "enabled": False,
            "base_token": "bascn_created",
            "table_id": "tbl_created",
            "created_base_name": "公众号文章",
            "created_table_name": "文章列表",
        }
    )
    save_config(config)
    monkeypatch.setattr(
        manage,
        "verify_feishu_identity",
        lambda *a, **k: {"status": "ready"},
    )
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *a, **k: pytest.fail("must not create a second Base"),
    )
    monkeypatch.setattr(
        manage,
        "grant_bot_created_resource",
        lambda *a, **k: (_ for _ in ()).throw(
            LarkCLIError("permission denied", kind="permission")
        ),
    )

    assert (
        manage.main(
            ["feishu-create-base", "--name", "公众号文章", "--table-name", "文章列表", "--yes"]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert "permission denied" in payload["error"]["message"]


def test_feishu_create_base_resume_rejects_name_mismatch(tmp_path, monkeypatch, capsys):
    import manage
    from config_store import load_config, save_config

    _configured(monkeypatch, tmp_path / "state")
    config = load_config()
    config["feishu"].update(
        {
            "provisioning": "created",
            "enabled": False,
            "base_token": "bascn_created",
            "table_id": "tbl_created",
            "created_base_name": "公众号文章",
            "created_table_name": "文章列表",
        }
    )
    save_config(config)
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *a, **k: pytest.fail("must not create a second Base"),
    )
    monkeypatch.setattr(
        manage,
        "grant_bot_created_resource",
        lambda *a, **k: pytest.fail("must not grant on mismatch"),
    )

    assert (
        manage.main(
            ["feishu-create-base", "--name", "另一个名字", "--table-name", "文章列表", "--yes"]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert "公众号文章" in payload["error"]["message"]


def test_feishu_create_base_still_refuses_foreign_target(tmp_path, monkeypatch, capsys):
    import manage
    from config_store import load_config, save_config

    _configured(monkeypatch, tmp_path / "state")
    config = load_config()
    config["feishu"].update(
        {
            "provisioning": "existing",
            "enabled": True,
            "base_token": "bascn_other",
            "table_id": "tbl_other",
        }
    )
    save_config(config)
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *a, **k: pytest.fail("must not create a second Base"),
    )
    assert (
        manage.main(
            ["feishu-create-base", "--name", "公众号文章", "--table-name", "文章列表", "--yes"]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert "already configured" in payload["error"]["message"]
