from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import subprocess
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))


def test_public_lark_execution_seam_keeps_isolated_runtime(monkeypatch: pytest.MonkeyPatch):
    import lark_runtime

    result = mock.Mock(returncode=0, stdout='{"ok":true,"data":{}}', stderr="")
    run = mock.Mock(return_value=result)
    monkeypatch.setattr(lark_runtime, "_lark_cli", lambda: "lark-cli")
    monkeypatch.setattr(lark_runtime.subprocess, "run", run)

    assert lark_runtime.run_lark(["config", "show"], retries=1) == {
        "ok": True,
        "data": {},
    }
    assert run.call_args.kwargs["cwd"] == lark_runtime.lark_cli_work_dir()
    assert run.call_args.kwargs["env"]["LARKSUITE_CLI_CONFIG_DIR"] == str(
        lark_runtime.lark_cli_config_dir()
    )

    run.reset_mock()
    assert lark_runtime.run_agent_lark(["--version"]) == (
        0,
        True,
        '{"ok":true,"data":{}}',
        "",
    )
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["timeout"] == 60
    assert run.call_args.kwargs["cwd"] == lark_runtime.lark_cli_work_dir()


def test_agent_lark_timeout_returns_sanitized_failure(monkeypatch, capsys):
    import lark_cli

    monkeypatch.setattr(
        lark_cli,
        "run_agent_lark",
        lambda arguments: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["lark-cli", *arguments], 60)
        ),
    )

    assert lark_cli.main(["--version"]) == 1
    assert capsys.readouterr().err == (
        "cannot run isolated lark-cli: command timed out after 60 seconds\n"
    )


def test_agent_lark_redacts_sensitive_arguments_from_failure(monkeypatch):
    import lark_runtime

    secret = "device-code-secret"
    monkeypatch.setattr(lark_runtime, "_lark_cli", lambda: "lark-cli")
    monkeypatch.setattr(
        lark_runtime.subprocess,
        "run",
        lambda *args, **kwargs: mock.Mock(
            returncode=1,
            stdout="",
            stderr=f"device authorization failed for {secret}",
        ),
    )

    result = lark_runtime.run_agent_lark(
        ["auth", "login", "--device-code", secret, "--json"]
    )
    assert result[:2] == (1, True)
    assert secret not in result[3]
    assert "<redacted>" in result[3]


def test_profile_store_redacts_global_metadata_and_preserves_source(tmp_path: Path):
    import lark_profile_store

    source = tmp_path / "global-config.json"
    original = {
        "apps": [
            {
                "name": "primary\nprofile",
                "appId": "cli_example123",
                "appSecret": "top-secret",
                "users": [{"openId": "ou_private"}],
            }
        ]
    }
    source.write_text(json.dumps(original), encoding="utf-8")
    before = source.read_bytes()
    result = lark_profile_store.discover(
        source,
        fingerprint_fn=lambda: lark_profile_store.fingerprint(source),
    )

    assert result["secrets_included"] is False
    assert result["profiles"][0]["name"] == "primaryprofile"
    assert "top-secret" not in json.dumps(result)
    assert source.read_bytes() == before


def test_domain_transition_invalidates_policy_and_resets_identity_state():
    import config_transitions
    from config_store import DEFAULT_CONFIG, load_config, save_config

    config = deepcopy(DEFAULT_CONFIG)
    config["redfox"] = {"api_key": "redfox-key"}
    config["subscriptions"] = [{"name": "Example", "alias": "example", "biz": ""}]
    config["setup"]["search_window_confirmed"] = True
    config["feishu"]["destination"] = "existing"
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "allow_feishu_sync": True,
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    save_config(config)

    result = config_transitions.transition("feishu_identity", "bot")
    saved = load_config()
    assert result["authorization"]["state"] == "not_required"
    assert saved["setup"]["feishu_identity_confirmed"] is True
    assert saved["setup"]["feishu_authorization"]["identity"] == "bot"
    assert saved["setup"]["execution_policy"]["confirmed"] is False
    assert saved["setup"]["execution_policy"]["allow_feishu_sync"] is False


def test_feishu_target_transition_invalidates_old_sync_approval():
    import config_transitions
    from config_store import DEFAULT_CONFIG, load_config, save_config

    config = deepcopy(DEFAULT_CONFIG)
    config["redfox"] = {"api_key": "redfox-key"}
    config["subscriptions"] = [{"name": "Example", "alias": "example", "biz": ""}]
    config["feishu"].update(
        {"destination": "existing", "enabled": True, "base_token": "basOld", "table_id": "tblOld"}
    )
    config["setup"]["execution_policy"].update(
        {"confirmed": True, "mode": "autopilot", "allow_feishu_sync": True}
    )
    save_config(config)

    config_transitions.transition(
        "feishu_target", {"base_token": "basNew", "table_id": "tblNew"}
    )

    saved = load_config()
    assert saved["feishu"]["base_token"] == "basNew"
    assert saved["feishu"]["table_id"] == "tblNew"
    assert saved["setup"]["execution_policy"]["confirmed"] is False
    assert saved["setup"]["execution_policy"]["allow_feishu_sync"] is False


def test_article_review_completion_seam_keeps_local_completion_without_sync(capsys):
    import process_pending
    from queue_helpers import add_pending, read_queue, record_verified_read

    article = {
        "link": "https://mp.weixin.qq.com/s/example",
        "title": "Example",
        "content": "reviewed",
    }
    add_pending([article])
    record_verified_read(article["link"], "reviewed")
    assert process_pending.main(
        ["done", "--link", article["link"], "--dims", json.dumps({
            "技术深度": 8,
            "信息新颖度": 8,
            "分析深度与独立观点": 8,
            "实用参考价值": 8,
            "内容质量与可信度": 8,
        }, ensure_ascii=False)]
    ) == 0
    saved = read_queue()
    assert not saved["pending"]
    assert next(iter(saved["processed"].values()))["sync_status"] == "not_requested"
    assert "Completed: Example" in capsys.readouterr().out
