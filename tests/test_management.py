from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "wechat-article-subscriber" / "scripts"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))


def configured() -> dict:
    from config_store import DEFAULT_CONFIG, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["redfox"] = {"api_key": "redfox-key-secret"}
    config["subscriptions"] = [{"name": "Example", "alias": "example"}]
    save_config(config)
    return config


def test_partial_settings_patch_preserves_other_values(monkeypatch: pytest.MonkeyPatch):
    import init_config
    from config_store import load_config

    configured()
    monkeypatch.setattr(init_config.sys, "stdin", io.StringIO('{"output_language":"zh"}'))
    assert init_config.main(["--agent-stdin", "--section", "settings", "--format", "json"]) == 0
    saved = load_config()
    assert saved["settings"]["output_language"] == "zh"
    assert saved["settings"]["check_hours"] == 24
    assert saved["setup"]["search_window_confirmed"] is False


def test_check_hours_patch_confirms_search_window(monkeypatch: pytest.MonkeyPatch, capsys):
    import init_config
    from config_store import load_config

    configured()
    monkeypatch.setattr(init_config.sys, "stdin", io.StringIO('{"check_hours":168}'))
    assert init_config.main(
        ["--agent-stdin", "--section", "settings", "--format", "json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_action"] == "ask_user_for_feishu_destination"
    assert load_config()["setup"]["search_window_confirmed"] is True


def test_partial_preferences_patch_preserves_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import init_config
    from config_store import load_config

    configured()
    monkeypatch.setattr(
        init_config.sys,
        "stdin",
        io.StringIO('{"include_topics":["AI Agent"],"digest_limit":8}'),
    )
    assert init_config.main(
        ["--agent-stdin", "--section", "preferences", "--format", "json"]
    ) == 0
    capsys.readouterr()
    saved = load_config()["preferences"]
    assert saved["include_topics"] == ["AI Agent"]
    assert saved["digest_limit"] == 8
    assert saved["digest_hours"] == 24


def test_setup_guide_explains_redfox_key_and_search_window(capsys):
    import init_config

    assert init_config.main(["--guide", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    data = payload["data"]
    assert payload["next_action"] == "ask_user_to_choose_chat_or_local_file"
    assert data["input_location"]["choose_one"] is True
    assert data["input_location"]["ordinary_chat_encrypted"] is False
    assert data["input_location"]["not_echoing_is_encryption"] is False
    assert "original chat message" in data["input_location"]["not_echoing_effect"]
    assert data["local_config_file"]["encrypted"] is False
    assert data["local_config_file"]["path"].endswith("config.json")
    assert data["local_config_file"]["required_fields"]["redfox.api_key"]
    assert data["local_config_file"]["minimal_template"]["redfox"] == {"api_key": ""}
    assert "wechat" not in json.dumps(data["local_config_file"]["minimal_template"])
    assert data["redfox_credentials"]["signup_url"] == "https://redfox.hk/"
    assert data["search_window"]["default_if_skipped"] == 24
    assert data["configuration_manifest"]["blocking_rule"]


def test_full_agent_setup_does_not_silently_skip_feishu(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import init_config
    from config_store import load_config

    configured()
    monkeypatch.setattr(
        init_config.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "redfox_api_key": "complete-key",
                    "settings": {"check_hours": 24},
                    "feishu": {},
                }
            )
        ),
    )
    assert init_config.main(["--agent-stdin", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_action"] == "ask_user_for_feishu_destination"
    assert payload["data"]["feishu_destination"] == "undecided"
    assert load_config()["feishu"]["destination"] == "undecided"


def test_full_agent_setup_rerun_preserves_confirmed_policy_and_feishu(
    monkeypatch: pytest.MonkeyPatch,
):
    import init_config
    from config_store import load_config

    configured()
    first = {
        "redfox_api_key": "complete-key",
        "feishu": {
            "destination": "existing",
            "enabled": True,
            "base_token": "bt-secret",
            "table_id": "tb-1",
            "binding_mode": "agent",
            "agent_source": "openclaw",
        },
        "execution_policy": {
            "mode": "autopilot",
            "confirmed": True,
            "allow_feishu_sync": True,
        },
        "settings": {"check_hours": 24},
    }
    monkeypatch.setattr(
        init_config.sys, "stdin", io.StringIO(json.dumps(first))
    )
    assert init_config.main(["--agent-stdin", "--format", "json"]) == 0
    assert load_config()["setup"]["execution_policy"]["confirmed"] is True
    assert load_config()["feishu"]["base_token"] == "bt-secret"

    # Host re-runs the full setup without repeating the confirmed policy and
    # the bound Feishu target: previously the whole config was rebuilt from
    # defaults and the confirmed policy plus binding were silently reset.
    rerun = {
        "redfox_api_key": "complete-key",
        "feishu": {"destination": "existing"},
    }
    monkeypatch.setattr(
        init_config.sys, "stdin", io.StringIO(json.dumps(rerun))
    )
    assert init_config.main(["--agent-stdin", "--format", "json"]) == 0
    saved = load_config()
    assert saved["setup"]["execution_policy"]["confirmed"] is True
    assert saved["setup"]["execution_policy"]["mode"] == "autopilot"
    assert saved["feishu"]["enabled"] is True
    assert saved["feishu"]["destination"] == "existing"
    assert saved["feishu"]["base_token"] == "bt-secret"
    assert saved["feishu"]["table_id"] == "tb-1"
    assert saved["settings"]["check_hours"] == 24


def test_full_agent_setup_partial_sections_merge_into_existing(
    monkeypatch: pytest.MonkeyPatch,
):
    import init_config
    from config_store import load_config

    configured()
    first = {
        "redfox_api_key": "complete-key",
        "feishu": {"destination": "skip"},
        "execution_policy": {
            "mode": "autopilot",
            "confirmed": True,
        },
        "settings": {"check_hours": 24, "output_language": "zh"},
    }
    monkeypatch.setattr(
        init_config.sys, "stdin", io.StringIO(json.dumps(first))
    )
    assert init_config.main(["--agent-stdin", "--format", "json"]) == 0

    # Re-run full setup sending only one settings key and a policy mode change:
    # omitted settings / policy fields must survive (merge semantics).
    rerun = {
        "redfox_api_key": "complete-key",
        "feishu": {"destination": "skip"},
        "execution_policy": {"mode": "guided"},
        "settings": {"check_hours": 12},
    }
    monkeypatch.setattr(
        init_config.sys, "stdin", io.StringIO(json.dumps(rerun))
    )
    assert init_config.main(["--agent-stdin", "--format", "json"]) == 0
    saved = load_config()
    assert saved["settings"]["check_hours"] == 12
    assert saved["settings"]["output_language"] == "zh"
    assert saved["setup"]["execution_policy"]["mode"] == "guided"
    assert saved["setup"]["execution_policy"]["confirmed"] is True


def test_setup_guide_manual_template_is_loadable(capsys):
    import init_config
    from config_store import load_config
    from paths import config_path

    assert init_config.main(["--guide", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    template = payload["data"]["local_config_file"]["minimal_template"]
    template["redfox"] = {"api_key": "complete-key"}
    template["subscriptions"] = [{"name": "Example"}]
    template["setup"]["search_window_confirmed"] = True
    target = config_path()
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(template), encoding="utf-8")
    config = load_config()
    assert config["redfox"]["api_key"] == "complete-key"
    assert config["setup"]["search_window_confirmed"] is True


def test_prepare_and_validate_local_file_never_overwrites(capsys):
    import init_config
    from paths import config_path

    assert init_config.main(["--prepare-local-file", "--format", "json"]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["data"]["created"] is True
    assert prepared["data"]["overwritten"] is False
    assert prepared["data"]["missing_fields"] == [
        "redfox.api_key",
        "subscriptions",
        "settings.check_hours confirmation",
        "feishu.destination confirmation",
    ]
    target = config_path()
    original = target.read_text(encoding="utf-8")
    assert init_config.main(["--prepare-local-file", "--format", "json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["data"]["created"] is False
    assert target.read_text(encoding="utf-8") == original


def test_open_local_file_uses_default_editor_without_reading_contents(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import init_config
    from paths import config_path

    assert init_config.main(["--prepare-local-file", "--format", "json"]) == 0
    capsys.readouterr()
    opened: list[Path] = []
    monkeypatch.setattr(init_config, "_launch_local_file", lambda path: opened.append(path))
    assert init_config.main(["--open-local-file", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert opened == [config_path()]
    assert payload["data"]["contents_echoed"] is False


def test_validate_local_file_is_complete_and_redacted(capsys):
    import init_config

    configured()
    assert init_config.main(["--validate-local-file", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(payload)
    assert payload["data"]["complete"] is False
    assert "redfox-key-secret" not in rendered
    assert payload["data"]["credentials_echoed"] is False


def test_config_migration_is_versioned_and_backed_up(tmp_path: Path):
    from config_store import CONFIG_VERSION, ConfigError, load_config, save_config
    from paths import config_path

    target = config_path()
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({
            "version": 1,
            "wechat": {"cookie": "old", "token": "1"},
            "subscriptions": [{"name": "Example"}],
        }),
        encoding="utf-8",
    )
    save_config(load_config())
    assert (target.parent / "config.v1.backup.json").is_file()
    assert load_config()["version"] == CONFIG_VERSION
    assert load_config()["feishu"]["destination"] == "undecided"
    future = json.loads(target.read_text(encoding="utf-8"))
    future["version"] = CONFIG_VERSION + 1
    target.write_text(json.dumps(future), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config()


def test_config_migration_recovers_old_confirmed_feishu_decision():
    from config_store import load_config
    from paths import config_path

    target = config_path()
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "version": 9,
                "wechat": {"cookie": "old", "token": "1"},
                "subscriptions": [{"name": "Example"}],
                "setup": {
                    "execution_policy": {
                        "confirmed": True,
                        "mode": "autopilot",
                        "allow_feishu_provisioning": False,
                        "provision_base_name": "",
                        "provision_table_name": "",
                        "allow_feishu_sync": False,
                        "approved_at": "2026-01-01T00:00:00+00:00",
                        "scope_version": 1,
                    }
                },
                "feishu": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    assert load_config()["feishu"]["destination"] == "skip"


def test_doctor_is_redacted_and_resumable(monkeypatch: pytest.MonkeyPatch, capsys):
    import manage

    configured()
    monkeypatch.setattr(
        manage,
        "lark_cli_info",
        lambda: {"path": "lark-cli", "version": "1.0.69", "tested_version": "1.0.69", "compatible": True},
    )
    assert manage.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    rendered = json.dumps(payload)
    assert "redfox-key-secret" not in rendered
    # With the redfox key configured, the next blocking step is the search
    # window; there is no wechat credential stage anymore.
    assert payload["data"]["setup_stage"] == "search_window_unconfirmed"
    assert payload["next_action"] == "ask_user_for_search_window"
    assert payload["data"]["progress"]["current_step"] == "search_window"
    assert payload["data"]["progress"]["next_action_label"]


def test_user_status_is_compact_and_actionable(monkeypatch: pytest.MonkeyPatch, capsys):
    import manage

    configured()
    monkeypatch.setattr(
        manage,
        "lark_cli_info",
        lambda: {"compatible": True, "version": "1.0.69"},
    )
    assert manage.main(["status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["data"]) == {
        "setup_stage",
        "progress",
        "paths",
        "config",
        "queue",
        "warnings",
    }
    assert payload["data"]["progress"]["percent"] > 0


def test_doctor_advances_past_redfox_to_feishu_destination(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    configured()
    config = load_config()
    config["setup"]["search_window_confirmed"] = True
    save_config(config)
    monkeypatch.setattr(
        manage,
        "lark_cli_info",
        lambda: {"compatible": True, "version": "1.0.69"},
    )
    assert manage.main(["doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["setup_stage"] == "feishu_destination_unconfirmed"
    assert payload["next_action"] == "ask_user_for_feishu_destination"


# test_recent_wechat_failure_overrides_stale_success removed: wechat health
# tracking was deleted together with the direct-connection data source.


def test_recent_feishu_failure_overrides_stale_success():
    import manage
    from config_store import validate_config

    config = configured()
    config["setup"]["search_window_confirmed"] = True
    config["setup"]["feishu_identity_confirmed"] = True
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "allow_feishu_sync": True,
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    config["subscriptions"][0]["biz"] = "biz"
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "identity": "bot",
            "expected_app_id": "cli_example123",
            "cli_profile": "wechat-article-profile",
            "manager_open_id": "ou_manager123",
            "base_token": "base_token",
            "table_id": "table_id",
        }
    )
    config["health"]["feishu"].update(
        {
            "last_verified_at": "2026-01-01T00:00:00+00:00",
            "last_failure_kind": "permission",
            "consecutive_failures": 1,
        }
    )
    validated = validate_config(config)
    stage, next_action = manage.next_stage(
        validated,
        cli={"compatible": True, "version": "1.0.69"},
    )
    assert stage == "feishu_validation_failed"
    assert next_action == "authorize_and_run_feishu_check"
    progress = manage._progress(
        validated,
        config_exists=True,
        config_valid=True,
        next_action=next_action,
    )
    assert progress["current_step"] == "feishu_validation"
    assert progress["percent"] < 100


def test_execution_policy_previews_once_then_persists(capsys):
    import manage
    from config_store import load_config

    configured()
    assert manage.main(["feishu-destination", "--mode", "skip"]) == 0
    capsys.readouterr()
    command = [
        "execution-policy",
        "set",
        "--mode",
        "autopilot",
        "--feishu-provisioning",
        "deny",
        "--feishu-sync",
        "deny",
    ]
    assert manage.main(command) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["data"]["saved"] is False
    assert preview["next_action"] == "rerun_with_yes"
    assert load_config()["setup"]["execution_policy"]["confirmed"] is False

    assert manage.main([*command, "--yes"]) == 0
    saved = json.loads(capsys.readouterr().out)
    policy = load_config()["setup"]["execution_policy"]
    assert saved["data"]["additional_routine_confirmations_required"] is False
    assert policy["confirmed"] is True
    assert policy["mode"] == "autopilot"
    assert policy["approved_at"]


def test_execution_policy_rejects_undecided_feishu_destination(capsys):
    import manage

    configured()
    result = manage.main(
        [
            "execution-policy",
            "set",
            "--mode",
            "autopilot",
            "--feishu-provisioning",
            "deny",
            "--feishu-sync",
            "deny",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert "choose the Feishu destination" in payload["error"]["message"]


def test_execution_policy_invalidates_only_when_feishu_approval_scope_changes():
    from config_store import DEFAULT_CONFIG
    from execution_policy import invalidate_for_feishu_change

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    policy = config["setup"]["execution_policy"]
    policy.update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "allow_feishu_provisioning": True,
            "provision_base_name": "Articles",
            "provision_table_name": "Inbox",
            "allow_feishu_sync": True,
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    previous = dict(config["feishu"])

    assert invalidate_for_feishu_change(config, previous, dict(previous)) is False
    assert policy["confirmed"] is True

    updated = {**previous, "manager_open_id": "ou_changed"}
    assert invalidate_for_feishu_change(config, previous, updated) is True
    assert policy["confirmed"] is False
    assert policy["allow_feishu_provisioning"] is False
    assert policy["allow_feishu_sync"] is False
    assert policy["approved_at"] == ""


def test_execution_policy_partial_patch_preserves_omitted_fields(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import init_config
    from config_store import load_config, save_config

    config = configured()
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
    monkeypatch.setattr(
        init_config.sys,
        "stdin",
        io.StringIO('{"approved_at":"2026-02-01T00:00:00+00:00"}'),
    )
    assert (
        init_config.main(
            ["--agent-stdin", "--section", "execution_policy", "--format", "json"]
        )
        == 0
    )
    capsys.readouterr()
    saved = load_config()["setup"]["execution_policy"]
    assert saved["confirmed"] is True
    assert saved["allow_feishu_sync"] is True
    assert saved["approved_at"] == "2026-02-01T00:00:00+00:00"


def test_agent_source_detection_per_platform(monkeypatch: pytest.MonkeyPatch):
    import manage

    for name in (
        "OPENCLAW_HOME",
        "OPENCLAW_STATE_DIR",
        "OPENCLAW_GATEWAY_TOKEN",
        "HERMES_HOME",
        "HERMES_STATE_DIR",
        "LARK_CHANNEL",
        "LARK_CHANNEL_HOME",
        "LARK_CHANNEL_APP_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    assert manage._detect_agent_source() == ""

    monkeypatch.setenv("OPENCLAW_HOME", "/tmp/oc")
    assert manage._detect_agent_source() == "openclaw"
    monkeypatch.delenv("OPENCLAW_HOME")

    monkeypatch.setenv("HERMES_STATE_DIR", "/tmp/hermes")
    assert manage._detect_agent_source() == "hermes"
    monkeypatch.delenv("HERMES_STATE_DIR")

    monkeypatch.setenv("LARK_CHANNEL_APP_ID", "cli_x")
    assert manage._detect_agent_source() == "lark-channel"


def test_config_accepts_openclaw_and_hermes_agent_source(
    monkeypatch: pytest.MonkeyPatch,
):
    from config_store import load_config, save_config

    for source in ("openclaw", "hermes"):
        config = configured()
        config["feishu"].update({"binding_mode": "agent", "agent_source": source})
        save_config(config)
        assert load_config()["feishu"]["agent_source"] == source


def test_host_context_accepts_openclaw_via_agent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    config = configured()
    config["feishu"]["destination"] = "existing"
    save_config(config)
    context_path = tmp_path / "host-context.json"
    context_path.write_text(
        json.dumps(
            {
                "source": "openclaw",
                "app_id": "cli_abc",
                "sender_open_id": "ou_sender",
            }
        ),
        encoding="utf-8",
    )
    assert (
        manage.main(["feishu-host-context", "--agent-file", str(context_path)]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["source"] == "openclaw"
    assert load_config()["feishu"]["agent_source"] == "openclaw"


def test_feishu_context_self_heals_cli_profile_for_existing_binding(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    config = configured()
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "identity": "bot",
            "binding_mode": "existing",
            "expected_app_id": "cli_abc",
            "cli_profile": "wechat-article-stale",
            "base_token": "bas_x",
            "table_id": "tbl_x",
        }
    )
    save_config(config)
    monkeypatch.setattr(
        manage,
        "resolve_lark_profile",
        lambda app_id: {
            "profile": "cli_a95a41a64eb81ceb",
            "app_id": app_id,
            "matched_by": "test",
            "match_count": 1,
        },
    )
    monkeypatch.setattr(
        manage,
        "feishu_identity_context",
        lambda verify=False: {
            "identity": "bot",
            "app_id": "cli_abc",
            "app_id_unambiguous": True,
            "bot": {"available": True, "status": "ready"},
            "user": {"available": False, "status": "missing", "token_status": ""},
        },
    )
    assert manage.main(["feishu-context", "--verify"]) == 0
    capsys.readouterr()
    assert load_config()["feishu"]["cli_profile"] == "cli_a95a41a64eb81ceb"


def test_feishu_identity_change_invalidates_execution_policy(capsys):
    import manage
    from config_store import load_config, save_config

    config = configured()
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"]["destination"] = "skip"
    config["feishu"]["identity"] = "bot"
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    save_config(config)
    assert manage.main(["feishu-identity", "--as", "user"]) == 0
    capsys.readouterr()
    assert load_config()["setup"]["execution_policy"]["confirmed"] is False


def test_feishu_context_never_guesses_generic_agent_bot(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    configured()
    config = load_config()
    config["feishu"]["binding_mode"] = "agent"
    config["feishu"]["agent_source"] = "lark-channel"
    config["setup"]["feishu_identity_confirmed"] = True
    save_config(config)
    monkeypatch.setattr(
        manage,
        "feishu_identity_context",
        lambda verify=False: pytest.fail(
            "CLI default profile must not be read without the host App ID"
        ),
    )
    for name in ("LARK_CHANNEL", "LARK_CHANNEL_HOME", "LARK_CHANNEL_APP_ID"):
        monkeypatch.delenv(name, raising=False)
    assert manage.main(["feishu-context", "--verify"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["host_bot_context_required"] is True
    assert payload["data"]["default_profile_allowed"] is False
    assert payload["data"]["global_profiles_read"] is False
    assert payload["next_action"] == "import_current_feishu_bot_context"


def test_feishu_context_pins_profile_matching_current_conversation_app(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    configured()
    config = load_config()
    config["feishu"].update(
        {
            "destination": "existing",
            "identity": "bot",
            "binding_mode": "agent",
            "agent_source": "lark-channel",
            "expected_app_id": "cli_current",
            "cli_profile": "",
            "manager_open_id": "ou_manager",
        }
    )
    config["setup"]["feishu_identity_confirmed"] = True
    save_config(config)
    monkeypatch.setattr(
        manage,
        "resolve_lark_profile",
        lambda app_id: {
            "profile": "current-bot",
            "app_id": app_id,
            "matched_by": "current_conversation_app_id",
            "match_count": 1,
            "default_profile_ignored": True,
            "secrets_included": False,
        },
    )

    def identity_context(*, verify=False):
        assert load_config()["feishu"]["cli_profile"] == "current-bot"
        return {
            "app_id": "cli_current",
            "app_ids": ["cli_current"],
            "app_id_unambiguous": True,
            "user": {"available": False, "status": "", "token_status": ""},
            "bot": {"available": True, "status": "ready"},
        }

    monkeypatch.setattr(manage, "feishu_identity_context", identity_context)
    assert manage.main(["feishu-context", "--verify"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert load_config()["feishu"]["cli_profile"] == "current-bot"
    assert payload["data"]["profile_resolution"]["default_profile_ignored"] is True
    assert payload["data"]["selected_identity"] == "bot"
    assert payload["next_action"] == "confirm_feishu_app_and_bot"


def test_feishu_context_requires_identity_before_cli_authorization(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage

    configured()
    context = monkeypatch.setattr(
        manage,
        "feishu_identity_context",
        lambda verify=False: pytest.fail("CLI context must not run before identity choice"),
    )
    assert context is None
    assert manage.main(["feishu-context", "--verify"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["identity_required"] is True
    assert set(payload["data"]["choices"]) == {"user", "bot"}
    assert payload["next_action"] == "ask_feishu_identity_before_authorization"


def test_lark_channel_context_is_offered_before_manual_identity(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage

    configured()
    monkeypatch.setenv("LARK_CHANNEL", "1")
    assert manage.main(["feishu-context", "--verify"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["host_bot_context_available"] is True
    assert payload["data"]["required_host_fields"] == [
        "source",
        "app_id",
        "sender_open_id",
    ]
    assert payload["next_action"] == "import_current_feishu_bot_context"


def test_feishu_host_context_imports_bot_and_manager_without_echo(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    config = configured()
    config["feishu"]["destination"] = "existing"
    save_config(config)
    monkeypatch.setattr(
        manage.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "source": "lark-channel",
                    "app_id": "cli_currentbot123",
                    "sender_open_id": "ou_current_sender_private",
                }
            )
        ),
    )
    assert manage.main(["feishu-host-context", "--agent-stdin"]) == 0
    rendered = capsys.readouterr().out
    payload = json.loads(rendered)
    saved = load_config()
    assert payload["next_action"] == "bind_detected_feishu_bot"
    assert payload["data"]["identity"] == "bot"
    assert payload["data"]["sender_open_id_included"] is False
    assert "ou_current_sender_private" not in rendered
    assert saved["setup"]["feishu_identity_confirmed"] is True
    assert saved["setup"]["feishu_authorization"]["state"] == "not_required"
    assert saved["feishu"]["binding_mode"] == "agent"
    assert saved["feishu"]["agent_source"] == "lark-channel"
    assert saved["feishu"]["expected_app_id"] == "cli_currentbot123"
    assert saved["feishu"]["manager_open_id"] == "ou_current_sender_private"


def test_bot_identity_never_requests_user_authorization(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage

    configured()
    assert manage.main(["feishu-identity", "--as", "bot"]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["data"]["identity"] == "bot"
    assert "never start user authorization" in selected["data"]["authorization_policy"]
    assert manage.main(["feishu-context", "--verify"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["selected_identity"] == "bot"
    assert payload["data"]["global_profiles_read"] is False
    assert payload["next_action"] == "select_feishu_app"


def test_feishu_app_selection_uses_deterministic_private_profile(capsys):
    import manage
    from config_store import load_config

    configured()
    assert manage.main(["feishu-identity", "--as", "bot"]) == 0
    capsys.readouterr()
    assert manage.main(["feishu-app", "--app-id", "cli_example123"]) == 0
    payload = json.loads(capsys.readouterr().out)
    saved = load_config()["feishu"]
    assert payload["data"]["global_profiles_modified"] is False
    assert payload["data"]["private_profile"].startswith("wechat-article-")
    assert saved["cli_profile"] == payload["data"]["private_profile"]
    assert saved["expected_app_id"] == "cli_example123"


def test_feishu_section_update_preserves_derived_profile(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import init_config
    from config_store import load_config, save_config

    config = configured()
    config["feishu"].update(
        {
            "expected_app_id": "cli_example123",
            "cli_profile": "wechat-article-profile",
            "binding_mode": "existing",
        }
    )
    save_config(config)
    monkeypatch.setattr(
        init_config.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "enabled": False,
                    "identity": "bot",
                    "binding_mode": "existing",
                    "expected_app_id": "cli_example123",
                }
            )
        ),
    )
    assert init_config.main(
        ["--agent-stdin", "--section", "feishu", "--format", "json"]
    ) == 0
    capsys.readouterr()
    assert load_config()["feishu"]["cli_profile"] == "wechat-article-profile"


def test_bot_identity_preserves_configured_human_manager(capsys):
    import manage
    from config_store import load_config, save_config

    config = configured()
    config["feishu"]["manager_open_id"] = "ou_manager"
    save_config(config)
    assert manage.main(["feishu-identity", "--as", "bot"]) == 0
    capsys.readouterr()
    assert load_config()["feishu"]["manager_open_id"] == "ou_manager"


def test_feishu_manager_command_persists_without_echoing_open_id(capsys):
    import manage
    from config_store import load_config

    configured()
    assert manage.main(["feishu-identity", "--as", "bot"]) == 0
    capsys.readouterr()
    assert manage.main(["feishu-manager", "--open-id", "ou_manager_secret"]) == 0
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert payload["data"]["manager_configured"] is True
    assert payload["data"]["permission_for_new_bot_resources"] == "full_access"
    assert "ou_manager_secret" not in payload_text
    assert load_config()["feishu"]["manager_open_id"] == "ou_manager_secret"


def test_bot_created_resource_grants_manager_full_access(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import save_config

    config = configured()
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {
            "identity": "bot",
            "expected_app_id": "cli_expected",
            "manager_open_id": "ou_manager",
        }
    )
    save_config(config)
    verified: list[tuple[dict, str]] = []
    granted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        manage,
        "verify_feishu_identity",
        lambda value, identity=None: verified.append((value, identity)) or {"status": "ready"},
    )
    monkeypatch.setattr(
        manage,
        "grant_bot_created_resource",
        lambda token, resource_type, manager: granted.append(
            (token, resource_type, manager)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("base_token\n"))
    assert manage.main(
        [
            "feishu-grant-manager",
            "--token-stdin",
            "--type",
            "bitable",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert verified and verified[0][1] == "bot"
    assert granted == [("base_token", "bitable", "ou_manager")]
    assert payload["data"]["permission"] == "full_access"
    assert payload["data"]["manager_granted"] is True
    assert payload["data"]["manager_open_id_included"] is False


def test_bot_created_resource_requires_configured_manager(capsys, monkeypatch):
    import manage
    from config_store import save_config

    config = configured()
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update({"identity": "bot", "expected_app_id": "cli_expected"})
    save_config(config)
    monkeypatch.setattr(sys, "stdin", io.StringIO("base_token\n"))
    assert manage.main(
        ["feishu-grant-manager", "--token-stdin", "--type", "bitable"]
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "LARK_CONFIG"
    assert "manager" in payload["error"]["message"]


def test_feishu_create_base_previews_schema_without_cli(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage

    config = configured()
    config["feishu"]["destination"] = "create"
    from config_store import save_config
    save_config(config)
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *args, **kwargs: pytest.fail("preview must not create a Base"),
    )
    assert manage.main(
        [
            "feishu-create-base",
            "--name",
            "公众号文章",
            "--table-name",
            "文章列表",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    preview = payload["data"]["preview"]
    assert preview["field_count"] == 11
    assert preview["transport"].endswith("no shell JSON")
    assert payload["next_action"] == "confirm_execution_policy_then_rerun"
    authorization = payload["data"]["authorization_command"]
    assert "execution-policy set" in authorization
    assert "公众号文章" in authorization and "文章列表" in authorization
    # Authorization routes through the persisted policy only: the guidance
    # never suggests running feishu-create-base with --yes.
    assert "feishu-create-base" not in authorization
    assert payload["data"]["policy_match"] is False


def test_feishu_create_base_uses_internal_schema_and_saves_mapping(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    config = configured()
    config["setup"]["feishu_identity_confirmed"] = True
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
    monkeypatch.setattr(
        manage,
        "verify_feishu_identity",
        lambda *args, **kwargs: {"status": "ready"},
    )
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *args, **kwargs: {
            "ok": True,
            "data": {
                "base": {"app_token": "bascn_created"},
                "created_table_id": "tbl_created",
            },
        },
    )
    monkeypatch.setattr(
        manage,
        "grant_bot_created_resource",
        lambda *args, **kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        manage,
        "preflight_feishu",
        lambda *args, **kwargs: {
            "mapping": {
                "title": {"field_id": "fld_title", "name": "文章标题", "type": "text"},
                "url": {"field_id": "fld_url", "name": "文章链接", "type": "url"},
            }
        },
    )
    assert manage.main(
        [
            "feishu-create-base",
            "--name",
            "公众号文章",
            "--table-name",
            "文章列表",
            "--yes",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    saved = load_config()["feishu"]
    assert payload["data"]["created"] is True
    assert payload["data"]["manager_granted"] is True
    assert saved["base_token"] == "bascn_created"
    assert saved["table_id"] == "tbl_created"
    assert saved["enabled"] is True
    assert saved["field_mapping"]["title"]["field_id"] == "fld_title"


def test_feishu_create_base_uses_matching_persisted_policy_without_yes(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    config = configured()
    config["setup"]["feishu_identity_confirmed"] = True
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
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
    monkeypatch.setattr(manage, "verify_feishu_identity", lambda *a, **k: {"status": "ready"})
    monkeypatch.setattr(
        manage,
        "create_standard_base",
        lambda *a, **k: {
            "ok": True,
            "data": {
                "base": {"app_token": "bascn_created"},
                "created_table_id": "tbl_created",
            },
        },
    )
    monkeypatch.setattr(manage, "grant_bot_created_resource", lambda *a, **k: {"ok": True})
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
    assert manage.main(
        [
            "feishu-create-base",
            "--name",
            "公众号文章",
            "--table-name",
            "文章列表",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["created"] is True
    assert payload["data"]["authorization_source"] == "persisted_execution_policy"
    saved = load_config()
    assert saved["health"]["feishu"]["last_verified_at"]
    assert saved["setup"]["execution_policy"]["allow_feishu_provisioning"] is False
    assert payload["data"]["provisioning_approval_consumed"] is True


def test_user_authorization_start_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config

    configured()
    assert manage.main(["feishu-identity", "--as", "user"]) == 0
    capsys.readouterr()
    context = {
        "user": {"available": False, "status": "", "token_status": ""},
        "bot": {"available": False, "status": ""},
    }
    probe = monkeypatch.setattr(manage, "feishu_identity_context", lambda verify=False: context)
    assert probe is None
    assert manage.main(["feishu-auth", "start"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["data"]["new_authorization_started"] is True
    assert first["data"]["device_code_persisted"] is False
    assert manage.main(["feishu-auth", "start"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["data"]["new_authorization_started"] is False
    assert second["next_action"] == "resume_existing_user_base_authorization"
    assert load_config()["setup"]["feishu_authorization"]["state"] == "waiting"


def test_user_authorization_complete_records_verified_state(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config

    configured()
    assert manage.main(["feishu-identity", "--as", "user"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        manage,
        "feishu_identity_context",
        lambda verify=False: {
            "user": {"available": True, "status": "ready", "token_status": "valid"},
            "bot": {"available": False, "status": ""},
        },
    )
    assert manage.main(["feishu-auth", "complete"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["authorization_verified"] is True
    saved = load_config()["setup"]["feishu_authorization"]
    assert saved["state"] == "authorized"
    assert saved["completed_at"]


def test_expired_verified_authorization_starts_only_one_new_flow(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    from config_store import load_config, save_config

    configured()
    assert manage.main(["feishu-identity", "--as", "user"]) == 0
    capsys.readouterr()
    config = load_config()
    config["setup"]["feishu_authorization"].update(
        {
            "state": "authorized",
            "identity": "user",
            "completed_at": "2026-01-01T00:00:00+00:00",
        }
    )
    save_config(config)
    monkeypatch.setattr(
        manage,
        "feishu_identity_context",
        lambda verify=False: {
            "app_id_unambiguous": True,
            "user": {"available": False, "status": "", "token_status": "expired"},
            "bot": {"available": False, "status": ""},
        },
    )
    assert manage.main(["feishu-auth", "start"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["new_authorization_started"] is True
    saved = load_config()["setup"]["feishu_authorization"]
    assert saved["state"] == "waiting"
    assert saved["completed_at"] == ""


def test_bulk_add_subscriptions_is_atomic_and_deduplicated(tmp_path: Path, capsys):
    import manage
    from config_store import load_config

    configured()
    source = tmp_path / "subscriptions.json"
    source.write_text(
        json.dumps(["Example", "Second", {"name": "Third", "alias": "third"}]),
        encoding="utf-8",
    )
    assert manage.main(["subscriptions", "bulk-add", "--file", str(source), "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["data"]["added_count"] == 2
    assert preview["next_action"] == "review_and_apply_subscription_batch"
    assert load_config()["subscriptions"] == [{"name": "Example", "alias": "example"}]
    assert manage.main(["subscriptions", "bulk-add", "--file", str(source)]) == 0
    saved = load_config()["subscriptions"]
    assert [item["name"] for item in saved] == ["Example", "Second", "Third"]


def test_manage_preferences_set_show_and_clear(capsys):
    import manage

    configured()
    assert manage.main(
        [
            "preferences",
            "set",
            "--include-topic",
            "AI Agent",
            "--exclude-keyword",
            "招聘",
            "--preferred-account",
            "机器之心",
            "--digest-hours",
            "48",
            "--digest-limit",
            "8",
        ]
    ) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["data"]["preferences"]["digest_limit"] == 8
    assert updated["next_action"] == "generate_digest_plan"
    assert manage.main(["preferences", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["data"]["preferences"]["exclude_keywords"] == ["招聘"]
    assert manage.main(["preferences", "clear"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["next_action"] == "rerun_with_yes"
    assert manage.main(["preferences", "clear", "--yes"]) == 0
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["data"]["preferences"]["digest_limit"] == 5


def test_reset_previews_then_clears_credentials(capsys):
    import manage
    from config_store import load_config

    configured()
    assert manage.main(["reset", "--scope", "credentials"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["next_action"] == "rerun_with_yes"
    assert load_config()["redfox"]["api_key"] == "redfox-key-secret"
    assert manage.main(["reset", "--scope", "credentials", "--yes"]) == 0
    capsys.readouterr()
    saved = load_config()
    assert saved["redfox"] == {"api_key": ""}
    assert saved["subscriptions"] == [{"name": "Example", "alias": "example"}]


def test_all_data_reset_removes_ephemeral_artifacts_and_preserves_runtime(capsys):
    import manage
    from paths import config_path, lock_path, queue_path

    configured()
    root = config_path().parent
    queue_path().write_text('{"version":1,"pending":[],"processed":[]}\n', encoding="utf-8")
    lock_path().write_text("lock\n", encoding="utf-8")
    backup = root / "config.v5.backup.json"
    backup.write_text("{}\n", encoding="utf-8")
    inbox = root / ".agent-config-test.json"
    inbox.write_text("{}\n", encoding="utf-8")
    qr = root / "feishu-auth-qr.png"
    qr.write_bytes(b"png")
    legacy_fields = root / "fields.json"
    legacy_fields.write_text("{}\n", encoding="utf-8")
    unknown_state = root / "legacy-state"
    unknown_state.mkdir()
    (unknown_state / "state.json").write_text("{}\n", encoding="utf-8")
    unrelated = root / "unrelated-user-file.txt"
    unrelated.write_text("keep me\n", encoding="utf-8")
    cli_config = root / "lark-cli-config"
    cli_config.mkdir()
    (cli_config / "config.json").write_text("{}\n", encoding="utf-8")
    cli_home = root / "lark-cli-home"
    (cli_home / ".lark-cli").mkdir(parents=True)
    (cli_home / ".lark-cli" / "config.json").write_text("{}\n", encoding="utf-8")
    cli_work = root / "lark-cli-work"
    cli_work.mkdir()
    (cli_work / "feishu-auth-qr.png").write_bytes(b"png")
    runtime_marker = root / "venv" / "preserve.txt"
    runtime_marker.parent.mkdir()
    runtime_marker.write_text("keep\n", encoding="utf-8")

    assert manage.main(["reset", "--scope", "all-data"]) == 0
    preview = json.loads(capsys.readouterr().out)
    previewed = set(preview["data"]["preview"])
    assert str(inbox.resolve()) in previewed
    assert str(qr.resolve()) in previewed
    assert str(cli_config.resolve()) in previewed
    assert str(cli_home.resolve()) in previewed
    assert str(cli_work.resolve()) in previewed
    assert str(legacy_fields.resolve()) in previewed
    assert str(unknown_state.resolve()) not in previewed
    assert str(unrelated.resolve()) not in previewed
    assert runtime_marker.exists()

    assert manage.main(["reset", "--scope", "all-data", "--yes"]) == 0
    capsys.readouterr()
    for path in (
        config_path(),
        queue_path(),
        lock_path(),
        backup,
        inbox,
        qr,
        legacy_fields,
    ):
        assert not path.exists()
    assert not cli_config.exists()
    assert not cli_home.exists()
    assert not cli_work.exists()
    assert runtime_marker.exists()
    assert not legacy_fields.exists()
    assert unknown_state.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"


# test_subscription_resolution_reports_ambiguity_without_guessing removed:
# discover_only.resolve_subscriptions (WeChat account search) was deleted with
# the direct-connection data source.


def test_process_json_failure_is_structured(capsys):
    import process_pending

    assert process_pending.main(["--format", "json", "read", "1"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ARTICLE_NOT_FOUND"


def _direct_article(account: str = "New Account") -> dict:
    return {
        "title": "Direct article",
        "account": account,
        "account_id": "biz-direct",
        "digest": "Direct digest",
        "update_time": 1_700_000_000,
        "link": "https://mp.weixin.qq.com/s/direct",
        "text": "Untrusted body",
        "content": "Untrusted body",
        "content_source": "direct",
    }


def test_done_uses_persisted_feishu_sync_policy(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import process_pending
    from config_store import save_config
    from queue_helpers import add_pending, record_verified_read

    config = configured()
    config["setup"]["execution_policy"].update(
        {
            "confirmed": True,
            "mode": "autopilot",
            "allow_feishu_sync": True,
            "approved_at": "2026-01-01T00:00:00+00:00",
        }
    )
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "base_token": "bascn_target",
            "table_id": "tbl_target",
        }
    )
    save_config(config)
    document = _direct_article("Example")
    add_pending([document])
    record_verified_read(document["link"], "verified direct article")
    synced: list[str] = []
    monkeypatch.setattr(
        process_pending,
        "_sync_entry",
        lambda entry, dry_run=False: synced.append(entry["article"]["link"]),
    )
    dimensions = json.dumps(
        {
            "技术深度": 8,
            "信息新颖度": 8,
            "分析深度与独立观点": 8,
            "实用参考价值": 8,
            "内容质量与可信度": 8,
        },
        ensure_ascii=False,
    )
    assert process_pending.main(
        ["done", "--link", document["link"], "--dims", dimensions]
    ) == 0
    capsys.readouterr()
    assert synced == [document["link"]]


def test_lark_cli_version_range_is_reported(monkeypatch: pytest.MonkeyPatch):
    import bitable_client

    monkeypatch.setattr(bitable_client, "_lark_cli", lambda: "lark-cli")
    monkeypatch.setattr(
        bitable_client.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "lark-cli 2.0.0", "stderr": ""})(),
    )
    info = bitable_client.lark_cli_info()
    assert info["version"] == "2.0.0"
    assert info["compatible"] is False


def test_lark_confirmation_envelope_is_not_misclassified_as_api_error():
    import bitable_client

    error = bitable_client._payload_error(
        {
            "ok": False,
            "error": {
                "type": "confirmation",
                "subtype": "confirmation_required",
                "risk": "high-risk-write",
                "action": "drive +delete",
            },
        },
        ["drive", "+delete"],
    )
    assert error.kind == "confirmation_required"


def test_feishu_check_save_mapping_keeps_fresh_health(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import feishu_target
    import process_pending
    from config_store import load_config, save_config

    config = configured()
    config["feishu"].update(
        {
            "destination": "existing",
            "enabled": True,
            "base_token": "base_token",
            "table_id": "table_id",
        }
    )
    config["health"]["feishu"].update(
        {
            "last_verified_at": "",
            "last_failure_kind": "api",
            "consecutive_failures": 3,
        }
    )
    save_config(config)
    monkeypatch.setattr(
        feishu_target,
        "lark_cli_info",
        lambda: {"version": "1.0.69", "compatible": True},
    )
    mapping = {
        "title": {"field_id": "fld_title", "name": "Title", "type": "text"},
        "url": {"field_id": "fld_url", "name": "URL", "type": "url"},
    }
    monkeypatch.setattr(
        feishu_target,
        "preflight_feishu",
        lambda feishu: {
            "identity": "bot",
            "field_count": 2,
            "mapping": mapping,
        },
    )
    assert process_pending.cmd_feishu_check(save_mapping=True) == 0
    capsys.readouterr()
    saved = load_config()
    assert saved["feishu"]["field_mapping"] == mapping
    assert saved["health"]["feishu"]["last_verified_at"]
    assert saved["health"]["feishu"]["last_failure_kind"] == ""
    assert saved["health"]["feishu"]["consecutive_failures"] == 0


def test_sync_json_preserves_non_retryable_lark_failure(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import process_pending
    from bitable_client import LarkCLIError
    from queue_helpers import (
        add_pending,
        complete_article,
        pending_sync_entries,
    )

    configured()
    article = {
        "title": "Permission boundary",
        "link": "https://mp.weixin.qq.com/s/permission-boundary",
        "digest": "digest",
        "account": "Example",
        "update_time": 1,
    }
    assert add_pending([article]) == 1
    complete_article(
        article["link"],
        {"score": 8.0, "summary": "summary", "tags": []},
        sync_status="pending",
    )

    def fail_sync(entry, *, dry_run=False):
        raise LarkCLIError("Base permission is missing", kind="permission")

    monkeypatch.setattr(process_pending, "_sync_entry", fail_sync)
    assert (
        process_pending.main(
            ["--format", "json", "sync-feishu", "--all"]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "LARK_PERMISSION"
    assert payload["error"]["retryable"] is False
    assert len(pending_sync_entries()) == 1


def test_existing_lark_profile_scan_redacts_secrets_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lark_runtime

    source = tmp_path / "global" / "config.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "apps": [
                    {
                        "name": "existing\u001b[31m",
                        "appId": "cli_example123",
                        "appSecret": {
                            "source": "keychain",
                            "id": "secret-keychain-identifier",
                        },
                        "brand": "feishu",
                        "lang": "zh",
                        "defaultAs": "bot",
                        "strictMode": "bot",
                        "users": [
                            {
                                "openId": "ou_secret_user",
                                "accessToken": "secret-user-token",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = source.read_bytes()
    monkeypatch.setattr(lark_runtime, "global_lark_config_path", lambda: source)
    inventory = lark_runtime.discover_global_lark_profiles()
    rendered = json.dumps(inventory)
    assert inventory["profile_count"] == 1
    assert inventory["profiles"][0]["app_id"] == "cli_example123"
    assert inventory["profiles"][0]["authorized_user_count"] == 1
    assert "secret-keychain-identifier" not in rendered
    assert "ou_secret_user" not in rendered
    assert "secret-user-token" not in rendered
    assert "\\u001b" not in rendered
    assert source.read_bytes() == before


def test_existing_lark_profile_import_isolated_and_strips_user_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lark_runtime

    source = tmp_path / "global" / "config.json"
    private_dir = tmp_path / "private"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "apps": [
                    {
                        "name": "existing",
                        "appId": "cli_example123",
                        "appSecret": {
                            "source": "keychain",
                            "id": "secret-keychain-identifier",
                        },
                        "brand": "feishu",
                        "lang": "zh",
                        "defaultAs": "bot",
                        "strictMode": "bot",
                        "users": [
                            {
                                "openId": "ou_secret_user",
                                "accessToken": "secret-user-token",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = source.read_bytes()
    monkeypatch.setattr(lark_runtime, "global_lark_config_path", lambda: source)
    monkeypatch.setattr(lark_runtime, "lark_cli_config_dir", lambda: private_dir)
    result = lark_runtime.import_global_lark_profile(
        "cli_example123", "wechat-article-private"
    )
    assert result["imported"] is True
    assert result["source_config_unchanged"] is True
    assert result["user_tokens_imported"] is False
    imported = json.loads((private_dir / "config.json").read_text(encoding="utf-8"))
    assert imported["apps"][0]["name"] == "wechat-article-private"
    assert imported["apps"][0]["appId"] == "cli_example123"
    assert imported["apps"][0]["users"] == []
    assert source.read_bytes() == before


def test_lark_runtime_prefers_isolated_cli_and_config(tmp_path: Path, monkeypatch):
    import lark_runtime

    home = tmp_path / "state with spaces"
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(home))
    monkeypatch.delenv("WECHAT_LARK_CLI_PATH", raising=False)
    launcher = (
        home
        / "lark-cli"
        / "node_modules"
        / ".bin"
        / ("lark-cli.cmd" if os.name == "nt" else "lark-cli")
    )
    native = (
        home
        / "lark-cli"
        / "node_modules"
        / "@larksuite"
        / "cli"
        / "bin"
        / ("lark-cli.exe" if os.name == "nt" else "lark-cli")
    )
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native")
    monkeypatch.setattr(
        lark_runtime.shutil,
        "which",
        lambda name: pytest.fail("PATH must not win over isolated lark-cli"),
    )
    assert lark_runtime.resolve_lark_cli() == native.resolve()
    environment = lark_runtime.lark_cli_environment()
    assert environment["LARKSUITE_CLI_CONFIG_DIR"] == str(
        (home / "lark-cli-home" / ".lark-cli").resolve()
    )
    assert environment["HOME"] == str((home / "lark-cli-home").resolve())
    assert environment["USERPROFILE"] == str((home / "lark-cli-home").resolve())


def test_lark_runtime_ignores_dangerous_config_override(tmp_path: Path, monkeypatch):
    import lark_runtime

    home = tmp_path / "state"
    global_like = tmp_path / ".lark-cli"
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(home))
    monkeypatch.setenv("WECHAT_LARK_CLI_CONFIG_DIR", str(global_like))
    monkeypatch.setenv("LARKSUITE_CLI_APP_SECRET", "must-not-inherit")
    environment = lark_runtime.lark_cli_environment()
    assert environment["LARKSUITE_CLI_CONFIG_DIR"] == str(
        (home / "lark-cli-home" / ".lark-cli").resolve()
    )
    assert "LARKSUITE_CLI_APP_SECRET" not in environment


def test_safe_lark_arguments_pins_profile_and_blocks_profile_mutation(capsys):
    import lark_runtime
    from config_store import save_config

    config = configured()
    config["feishu"]["expected_app_id"] = "cli_example123"
    config["feishu"]["cli_profile"] = "wechat-article-profile"
    save_config(config)
    assert lark_runtime.safe_lark_arguments(["auth", "status", "--json"]) == [
        "--profile",
        "wechat-article-profile",
        "auth",
        "status",
        "--json",
    ]
    with pytest.raises(ValueError, match="profile mutation"):
        lark_runtime.safe_lark_arguments(["profile", "use", "another"])
    capsys.readouterr()


def test_agent_bind_is_pinned_to_imported_host_app_and_source():
    import lark_runtime
    from config_store import load_config, save_config

    configured()
    config = load_config()
    config["feishu"].update(
        {
            "destination": "existing",
            "binding_mode": "agent",
            "agent_source": "lark-channel",
            "expected_app_id": "cli_currentbot123",
        }
    )
    save_config(config)
    expected = [
        "config",
        "bind",
        "--source",
        "lark-channel",
        "--app-id",
        "cli_currentbot123",
        "--identity",
        "user-default",
    ]
    assert lark_runtime.safe_lark_arguments(expected) == expected
    with pytest.raises(ValueError, match="does not match"):
        lark_runtime.safe_lark_arguments(
            [
                "config",
                "bind",
                "--source",
                "lark-channel",
                "--app-id",
                "cli_otherbot",
            ]
        )


def test_config_init_is_forced_to_confirmed_named_profile():
    import lark_runtime
    from config_store import save_config

    config = configured()
    config["feishu"].update(
        {
            "expected_app_id": "cli_example123",
            "cli_profile": "wechat-article-profile",
        }
    )
    save_config(config)
    args = lark_runtime.safe_lark_arguments(
        [
            "config",
            "init",
            "--app-id",
            "cli_example123",
            "--app-secret-stdin",
        ]
    )
    assert args[-2:] == ["--name", "wechat-article-profile"]
    with pytest.raises(ValueError, match="blocked"):
        lark_runtime.safe_lark_arguments(["config", "init", "--new"])


def test_explicit_lark_cli_path_never_falls_back_to_global(tmp_path: Path, monkeypatch):
    import lark_runtime

    monkeypatch.setenv("WECHAT_LARK_CLI_PATH", str(tmp_path / "missing-lark-cli"))
    monkeypatch.setattr(
        lark_runtime.shutil,
        "which",
        lambda name: pytest.fail("invalid explicit path must not fall back to PATH"),
    )
    with pytest.raises(FileNotFoundError, match="WECHAT_LARK_CLI_PATH"):
        lark_runtime.resolve_lark_cli()


def test_redacted_config_reports_manager_without_exposing_open_id():
    from config_store import redacted_config

    config = configured()
    config["feishu"]["manager_open_id"] = "ou_manager_secret"
    rendered = json.dumps(redacted_config(config))
    assert '"manager_configured": true' in rendered
    assert "ou_manager_secret" not in rendered


@pytest.mark.skipif(os.name != "nt", reason="Windows custom installer test")
def test_windows_custom_install_path(tmp_path: Path):
    powershell = "powershell"
    destination = tmp_path / "custom" / "wechat-article-subscriber"
    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(ROOT / "install.ps1"),
            "-Target",
            "agents",
            "-InstallPath",
            str(destination),
            "-NoDeps",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert (destination / "SKILL.md").is_file()


def test_next_stage_requires_bot_app_secret_before_manager():
    from config_store import DEFAULT_CONFIG
    from execution_policy import next_stage

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["redfox"]["api_key"] = "key"
    config["setup"]["search_window_confirmed"] = True
    config["subscriptions"] = [{"name": "Example", "alias": "example"}]
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "expected_app_id": "cli_x",
            "cli_profile": "p1",
        }
    )
    config["setup"]["feishu_identity_confirmed"] = True
    cli = {
        "compatible": True,
        "profile_secret": {"bound": True, "profile": "p1", "ready": False},
    }
    stage, action = next_stage(config, cli=cli)
    assert stage == "feishu_secret_missing"
    assert action == "provide_app_secret_for_private_profile"

    cli_ready = {
        "compatible": True,
        "profile_secret": {"bound": True, "profile": "p1", "ready": True},
    }
    stage, action = next_stage(config, cli=cli_ready)
    assert stage == "feishu_manager_missing"

    # Legacy cli payloads without the secret field keep their old stages.
    stage, action = next_stage(config, cli={"compatible": True})
    assert stage == "feishu_manager_missing"


def test_manage_next_surfaces_bot_secret_question(monkeypatch: pytest.MonkeyPatch):
    import manage

    configured()
    config = manage.load_config()
    config["setup"]["search_window_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "expected_app_id": "cli_x",
            "cli_profile": "p1",
        }
    )
    config["setup"]["feishu_identity_confirmed"] = True
    manage.save_config(config)
    monkeypatch.setattr(
        manage,
        "lark_cli_info",
        lambda: {
            "compatible": True,
            "profile_secret": {"bound": True, "profile": "p1", "ready": False},
        },
    )
    state, action = manage._next_step()
    assert state["stage"] == "feishu_secret_missing"
    assert action == "provide_app_secret_for_private_profile"
    assert "feishu-app-secret" in state["command"]
    assert "App Secret" in state["question"]


def test_wizard_provisioning_command_never_suggests_bare_create_yes(
    isolated_home,
):
    import manage
    from config_store import save_config
    from lark_runtime import lark_cli_config_dir
    from paths import secure_write_json

    config = configured()
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "expected_app_id": "cli_x",
            "cli_profile": "p1",
            "manager_open_id": "ou_manager",
        }
    )
    save_config(config)
    lark_cli_config_dir().mkdir(parents=True, exist_ok=True)
    secure_write_json(
        lark_cli_config_dir() / "config.json",
        {"apps": [{"name": "p1", "appId": "cli_x", "appSecret": {"source": "keychain", "id": "k1"}}]},
    )
    state, action = manage._feishu_setup()
    assert action == "provision_configured_feishu_base"
    command = state["next_command"]
    assert "feishu-create-base --name <名称> --table-name <表名>" in command
    assert "feishu-create-base --name <名称> --table-name <表名> --yes" not in command
    assert "策略同名精确匹配即自动授权" in command


def test_reset_credentials_preview_lists_configured_fields(capsys):
    import manage
    from config_store import load_config, save_config

    config = configured()
    config["setup"]["feishu_identity_confirmed"] = True
    config["setup"]["execution_policy"].update({"confirmed": True})
    config["feishu"].update(
        {
            "identity": "bot",
            "expected_app_id": "cli_x",
            "cli_profile": "p1",
            "manager_open_id": "ou_manager",
            "destination": "create",
        }
    )
    save_config(config)
    assert manage.main(["reset", "--scope", "credentials"]) == 0
    payload = json.loads(capsys.readouterr().out)
    preview = payload["data"]["preview"]
    assert "redfox.api_key" in preview
    assert "setup.execution_policy" in preview
    assert "feishu.expected_app_id" in preview
    assert "feishu.manager_open_id" in preview
    assert "feishu.destination" in preview
    # Nothing was cleared by the preview itself.
    assert load_config()["redfox"]["api_key"] == "redfox-key-secret"


def test_doctor_online_failure_returns_error_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage
    import redfox_client
    from redfox_client import RedfoxAuthError

    configured()

    class FakeClient:
        def __init__(self, api_key, **kwargs):
            pass

        def query_work_list(self, **kwargs):
            raise RedfoxAuthError("redfox API rejected the request: API Key无效")

        def close(self):
            pass

    monkeypatch.setattr(redfox_client, "RedfoxClient", FakeClient)
    assert manage.main(["doctor", "--online"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REDFOX_AUTH"
    assert payload["error"]["next_action"] == "run_redfox_key_setup"
    assert payload["data"]["online"]["redfox"]["ok"] is False
    assert payload["data"]["online"]["redfox"]["error"]["code"] == "REDFOX_AUTH"


def test_doctor_online_missing_key_fails_loudly(capsys):
    import manage

    configured()
    config = manage.load_config()
    config["redfox"]["api_key"] = ""
    manage.save_config(config)
    assert manage.main(["doctor", "--online"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REDFOX_AUTH"


def test_redfox_status_verify_failure_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    import manage

    configured()
    monkeypatch.setattr(
        manage,
        "_probe_redfox",
        lambda api_key: {
            "reachable": False,
            "error_code": "REDFOX_AUTH",
            "message": "redfox API rejected the request: API Key无效",
        },
    )
    assert manage.main(["redfox-status", "--verify"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REDFOX_AUTH"
    assert payload["error"]["next_action"] == "run_redfox_key_setup"
    assert payload["data"]["reachable"] is False
    # Without --verify the same state stays informational.
    monkeypatch.setattr(
        manage, "_probe_redfox", lambda api_key: pytest.fail("must not probe")
    )
    assert manage.main(["redfox-status"]) == 0
    informational = json.loads(capsys.readouterr().out)
    assert informational["ok"] is True
    assert informational["data"]["reachable"] is None


def test_manage_accepts_format_flag_after_subcommand(capsys):
    import manage

    configured()
    assert manage.main(["doctor", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert manage.main(["--format", "json", "config-show"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_payload_error_not_configured_maps_to_secret_fix():
    from bitable_client import LarkCLIError, _payload_error

    error = _payload_error({"error": {"type": "config", "message": "not configured"}}, [])
    assert isinstance(error, LarkCLIError)
    assert error.kind == "config"
    assert "feishu-app-secret" in str(error)


def test_payload_error_generic_failure_includes_raw_response():
    from bitable_client import _payload_error

    error = _payload_error({"ok": False, "code": 9999, "error": ""}, [])
    message = str(error)
    assert "raw response" in message
    assert "9999" in message


def test_private_profile_secret_state_is_local_only():
    from config_store import load_config, save_config
    from lark_runtime import lark_cli_config_dir, private_profile_secret_state
    from paths import secure_write_json

    # Unbound: no app selected yet.
    configured()
    state = private_profile_secret_state()
    assert state == {
        "bound": False,
        "profile": "",
        "app_secret_storage": "unbound",
        "ready": False,
    }

    # Bound app without an isolated profile: the missing-secret dead end.
    config = load_config()
    config["setup"]["feishu_identity_confirmed"] = True
    config["feishu"].update(
        {"identity": "bot", "expected_app_id": "cli_x", "cli_profile": "p1"}
    )
    save_config(config)
    state = private_profile_secret_state()
    assert state["bound"] is True
    assert state["ready"] is False
    assert state["app_secret_storage"] == "missing"

    # Profile with an inline secret becomes ready without any lark-cli call.
    lark_cli_config_dir().mkdir(parents=True, exist_ok=True)
    secure_write_json(
        lark_cli_config_dir() / "config.json",
        {"apps": [{"name": "p1", "appId": "cli_x", "appSecret": "inline-secret"}]},
    )
    state = private_profile_secret_state()
    assert state["ready"] is True
    assert state["app_secret_storage"] == "inline"


def test_bot_identity_readiness_maps_to_bot_credentials_next_action(
    monkeypatch: pytest.MonkeyPatch,
):
    import bitable_client
    from protocol import failure

    monkeypatch.setattr(
        bitable_client,
        "_run_lark",
        lambda args, **kwargs: {
            "data": {
                "appid": "cli_x",
                "identities": {
                    "bot": {"available": True, "status": "not_ready"}
                },
            }
        },
    )
    feishu = {
        "identity": "bot",
        "expected_app_id": "cli_x",
        "binding_mode": "existing",
    }
    try:
        bitable_client.verify_feishu_identity(feishu, identity="bot")
    except Exception as exc:  # noqa: BLE001 - classified below
        envelope = failure(exc)
        assert envelope["error"]["code"] == "LARK_BOT_CREDENTIALS"
        assert (
            envelope["error"]["next_action"]
            == "configure_bot_credentials_and_scopes_without_user_auth"
        )
        assert "do not run user auth" in envelope["error"]["message"]
    else:
        raise AssertionError("bot identity without a ready profile must fail")


def test_manage_next_target_pending_routes_through_policy(
    monkeypatch: pytest.MonkeyPatch,
):
    import manage

    configured()
    config = manage.load_config()
    config["setup"]["search_window_confirmed"] = True
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "expected_app_id": "cli_x",
            "cli_profile": "p1",
            "manager_open_id": "ou_manager",
        }
    )
    config["setup"]["feishu_identity_confirmed"] = True
    manage.save_config(config)
    monkeypatch.setattr(
        manage,
        "lark_cli_info",
        lambda: {
            "compatible": True,
            "profile_secret": {"bound": True, "profile": "p1", "ready": True},
        },
    )
    state, action = manage._next_step()
    assert state["stage"] == "feishu_target_pending"
    assert action == "provision_configured_feishu_base"
    command = state["command"]
    assert "execution-policy set" in command
    assert "feishu-create-base --name <已批准名> --table-name <已批准名>" in command
    assert (
        "feishu-create-base --name <已批准名> --table-name <已批准名> --yes"
        not in command
    )


def test_agent_binding_skips_app_secret_gate(monkeypatch: pytest.MonkeyPatch):
    import manage
    from config_store import DEFAULT_CONFIG
    from execution_policy import next_stage

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["redfox"]["api_key"] = "key"
    config["setup"]["search_window_confirmed"] = True
    config["subscriptions"] = [{"name": "Example", "alias": "example"}]
    config["feishu"].update(
        {
            "destination": "create",
            "identity": "bot",
            "binding_mode": "agent",
            "agent_source": "lark-channel",
            "expected_app_id": "cli_x",
            "cli_profile": "",
            "manager_open_id": "ou_manager",
        }
    )
    config["setup"]["feishu_identity_confirmed"] = True
    # Host-provided credentials: an unready local profile must not trigger the
    # App Secret question for an agent binding.
    cli = {
        "compatible": True,
        "profile_secret": {"bound": False, "profile": "", "ready": False},
    }
    stage, _ = next_stage(config, cli=cli)
    assert stage == "feishu_target_pending"

    monkeypatch.setattr(
        manage,
        "lark_cli_info",
        lambda: {
            "compatible": True,
            "profile_secret": {"bound": False, "profile": "", "ready": False},
        },
    )
    from config_store import save_config

    save_config(config)
    state, action = manage._feishu_setup()
    assert action != "provide_app_secret_for_private_profile"
