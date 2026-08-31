#!/usr/bin/env python3
"""Inspect, patch, diagnose, and safely reset skill state."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from article_inbox import queue_summary
from bitable_client import (
    LarkCLIError,
    create_standard_base,
    probe_app_secret_resolution,
    created_base_identifiers,
    feishu_identity_context,
    grant_bot_created_resource,
    lark_cli_info,
    preflight_feishu,
    resolve_lark_profile,
    standard_field_schema,
    verify_feishu_identity,
)
from config_store import (
    DEFAULT_CONFIG,
    save_config,
    ConfigError,
    load_config,
    modify_config,
    redacted_config,
    update_health,
    validate_config,
)
from execution_policy import (
    allows_automatic_provisioning,
    invalidate_policy,
    next_stage,
    policy_for,
)
from feishu_target import production_feishu_target
from paths import config_path, data_dir, lock_path, queue_path, venv_dir
from lark_runtime import (
    discover_global_lark_profiles,
    import_global_lark_profile,
    profile_name_for_app,
)
from protocol import dump, failure, success


STEP_LABELS = {
    "feishu_destination": "确认是否写入飞书多维表格",
    "local_config": "准备本地配置文件",
    "redfox_credentials": "配置 redfox API Key",
    "search_window": "确认文章搜索时间范围",
    "subscriptions": "添加订阅公众号",
    "subscription_resolution": "确认公众号匹配结果",
    "execution_policy": "确认一次性自动执行范围",
    "feishu_identity": "选择飞书执行身份",
    "feishu_authorization": "完成飞书身份授权",
    "feishu_target": "确认飞书目标表格",
    "feishu_validation": "验证飞书身份与目标表格",
}

ACTION_LABELS = {
    "ask_user_for_feishu_destination": "选择跳过飞书、映射现有多维表格或创建新表",
    "import_current_feishu_bot_context": "从当前飞书机器人会话导入 App ID 和发送者 Open ID",
    "bind_detected_feishu_bot": "绑定当前飞书会话的机器人应用",
    "repair_local_config_file": "修复本地配置文件中的 JSON 或字段错误",
    "edit_local_config_file": "填写并保存本地配置文件",
    "run_online_doctor": "在线体检 redfox API 与飞书",
    "run_redfox_key_setup": "通过 stdin 设置 redfox API Key",
    "confirm_daily_run": "确认以上计划后加 --yes 执行每日发现与简报",
    "ask_user_for_search_window": "选择文章搜索时间范围",
    "ask_for_subscription_names": "添加至少一个公众号",
    "edit_subscriptions_add_alias": "为缺少微信号的订阅补充 alias（广域库仅认微信号）",
    "ask_user_to_choose_chat_or_local_file": "选择在聊天中配置，或编辑本地配置文件",
    "run_feishu_context_then_authorize_only_if_needed": "验证飞书上下文，仅在缺失时发起授权",
    "review_and_apply_subscription_batch": "检查批量订阅预览并确认写入",
    "review_and_confirm_execution_policy": "一次确认后续自动执行范围",
    "ask_feishu_identity_before_authorization": "选择个人用户或机器人身份",
    "run_feishu_auth_start": "检查现有飞书授权；仅在缺失时发起一次授权",
    "resume_existing_user_base_authorization": "继续当前飞书授权，不要重新发起",
    "check_or_install_lark_cli": "检查或安装兼容的飞书 CLI",
    "ask_user_for_feishu_setup_choice": "本地未发现 lark-cli 或应用配置：询问用户如何继续（安装 / 提供应用信息 / 跳过飞书），不扩大检索范围",
    "install_compatible_lark_cli": "安装兼容的飞书 CLI 版本",
    "authorize_and_run_feishu_check": "完成飞书只读检查",
    "resolve_and_save_feishu_manager": "确认接收机器人文件管理权限的飞书用户",
    "select_feishu_app": "选择并固定本技能要使用的飞书 App ID",
    "configure_private_lark_profile": "在技能私有目录中配置已选飞书应用",
    "provide_app_secret_for_private_profile": "从飞书开放平台复制 App Secret 并经 stdin 初始化私有配置",
    "provision_configured_feishu_base": "自动创建并验证已批准的飞书多维表格",
    "configure_existing_feishu_target": "配置一个明确的现有飞书目标表格",
    "rerun_with_yes_or_update_execution_policy": "确认本次创建，或更新一次性自动执行范围",
    "continue_setup_then_execute": "继续完成配置并自动执行任务",
    "discover_articles": "发现并查看新文章",
}


def _authorization(config: dict[str, Any]) -> dict[str, Any]:
    return config["setup"]["feishu_authorization"]


def _reset_authorization(config: dict[str, Any], identity: str) -> None:
    state = "not_required" if identity == "bot" else "not_started"
    config["setup"]["feishu_authorization"] = {
        **dict(DEFAULT_CONFIG["setup"]["feishu_authorization"]),
        "state": state,
        "identity": identity,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _progress(
    config: dict[str, Any] | None,
    *,
    config_exists: bool,
    config_valid: bool,
    next_action: str,
) -> dict[str, Any]:
    checks: list[tuple[str, bool, bool]] = [
        ("local_config", config_exists and config_valid, False),
    ]
    if config is None:
        checks.extend(
            (step, False, False)
            for step in (
                "redfox_credentials",
                "search_window",
                "subscriptions",
                "subscription_resolution",
                "feishu_destination",
                "execution_policy",
            )
        )
    else:
        subscriptions = config["subscriptions"]
        checks.extend(
            [
                (
                    "redfox_credentials",
                    bool(config["redfox"]["api_key"].strip()),
                    False,
                ),
                ("search_window", bool(config["setup"]["search_window_confirmed"]), False),
                ("subscriptions", bool(subscriptions), False),
                (
                    "subscription_resolution",
                    bool(subscriptions)
                    and all(str(item.get("alias", "")).strip() for item in subscriptions),
                    False,
                ),
                (
                    "feishu_destination",
                    config["feishu"]["destination"] != "undecided",
                    False,
                ),
                (
                    "execution_policy",
                    bool(policy_for(config)["confirmed"]),
                    False,
                ),
            ]
        )
        feishu_requested = config["feishu"]["destination"] in {"existing", "create"}
        if feishu_requested:
            identity = config["feishu"]["identity"]
            authorization_ready = (
                identity == "bot"
                or _authorization(config)["state"] in {"authorized", "not_required"}
            )
            feishu_health = config["health"]["feishu"]
            feishu_ready = bool(feishu_health["last_verified_at"]) and not bool(
                feishu_health["consecutive_failures"]
            )
            checks.extend(
                [
                    ("feishu_identity", bool(config["setup"]["feishu_identity_confirmed"]), False),
                    ("feishu_authorization", authorization_ready, False),
                    (
                        "feishu_target",
                        bool(config["feishu"]["base_token"] and config["feishu"]["table_id"]),
                        False,
                    ),
                    ("feishu_validation", feishu_ready, False),
                ]
            )
        else:
            checks.extend(
                (step, False, True)
                for step in (
                    "feishu_identity",
                    "feishu_authorization",
                    "feishu_target",
                    "feishu_validation",
                )
            )
    first_incomplete = next(
        (step for step, complete, optional in checks if not complete and not optional),
        "",
    )
    steps = []
    for step, complete, optional in checks:
        if optional:
            status = "optional"
        elif complete:
            status = "complete"
        elif step == first_incomplete:
            status = "current"
        else:
            status = "pending"
        steps.append({"id": step, "label": STEP_LABELS[step], "status": status})
    required = [item for item in steps if item["status"] != "optional"]
    complete_count = sum(item["status"] == "complete" for item in required)
    return {
        "completed": complete_count,
        "total": len(required),
        "percent": round(complete_count * 100 / len(required)) if required else 100,
        "current_step": first_incomplete,
        "steps": steps,
        "next_action": next_action,
        "next_action_label": ACTION_LABELS.get(next_action, next_action),
    }


def _expected_app_id(config: dict[str, Any]) -> str:
    """Return the saved Feishu App ID, normalized."""
    return str(config["feishu"].get("expected_app_id") or "").strip()


def _doctor(*, online: bool) -> tuple[dict[str, Any], str]:
    report: dict[str, Any] = {
        "runtime": {
            "python": platform.python_version(),
            "supported": sys.version_info >= (3, 9),
            "dependencies": {
                name: importlib.util.find_spec(name) is not None
                for name in ("requests",)
            },
        },
        "paths": {
            "data_dir": str(data_dir()),
            "config": str(config_path()),
            "queue": str(queue_path()),
            "venv": str(venv_dir()),
        },
        "transport": {
            "recommended": "offer ordinary chat or direct local config-file editing",
            "stdin_supported": True,
            "one_time_inbox_supported": True,
            "command_line_secrets_supported": False,
            "config_file": str(config_path()),
            "config_file_encrypted": False,
            "ordinary_chat_encrypted": False,
            "not_echoing_is_encryption": False,
            "ordinary_chat_retention_possible": True,
            "not_echoing_effect": (
                "reduces repeat exposure in Agent output but does not remove or protect "
                "the original chat message"
            ),
        },
    }
    try:
        config = load_config()
    except ConfigError as exc:
        exists = config_path().exists()
        next_action = "repair_local_config_file" if exists else "ask_user_to_choose_chat_or_local_file"
        report["config"] = {"exists": exists, "valid": False, "message": str(exc)}
        report["setup_stage"] = "config_invalid" if exists else "config_missing"
        report["progress"] = _progress(
            None,
            config_exists=exists,
            config_valid=False,
            next_action=next_action,
        )
        return report, next_action

    report["config"] = {"exists": True, **redacted_config(config)}
    report["warnings"] = []
    if (
        config["settings"]["check_hours"] > 48
        and config["settings"]["max_articles_per_account"] <= 10
    ):
        report["warnings"].append(
            {
                "kind": "search_window_coverage",
                "message": (
                    "The lookback window exceeds 48 hours while the per-account limit is "
                    "10 or lower; busy accounts may not be covered completely."
                ),
                "next_action": "increase_max_articles_per_account_or_reduce_search_window",
            }
        )
    summary = queue_summary()
    report["queue"] = {
        "total": summary["pending"] + summary["processed"],
        **summary,
    }
    cli: dict[str, Any] | None = None
    try:
        cli = lark_cli_info()
        report["lark_cli"] = cli
    except LarkCLIError as exc:
        report["lark_cli"] = {
            "available": False,
            "error_kind": exc.kind,
            "message": str(exc),
        }

    online_report: dict[str, Any] = {}
    if online:
        from redfox_client import RedfoxClient

        api_key = config["redfox"]["api_key"].strip()
        if not api_key:
            online_report["redfox"] = {
                "ok": False,
                "error_code": "REDFOX_AUTH",
                "message": "redfox API key is missing; run redfox-set-key",
            }
        else:
            client = RedfoxClient(api_key)
            try:
                # The probe only proves the key authenticates and the
                # service answers; a code=0 response with an empty list is
                # still a pass. Connectivity, not coverage.
                client.query_work_list(account="probe", offset=0, count=1)
                online_report["redfox"] = {"ok": True}
            except Exception as exc:
                online_report["redfox"] = failure(exc)["error"]
            finally:
                client.close()
        if config["feishu"]["enabled"]:
            try:
                result = production_feishu_target(config["feishu"]).check()
                update_health("feishu", success=True)
                online_report["feishu"] = {"ok": True, "preflight": result}
            except Exception as exc:
                try:
                    update_health("feishu", success=False, failure_kind=getattr(exc, "kind", type(exc).__name__))
                except Exception:
                    pass
                online_report["feishu"] = failure(exc)["error"]
        report["online"] = online_report

    config = load_config()
    stage, next_action = next_stage(config, cli=cli)
    report["setup_stage"] = stage
    report["health"] = config["health"]
    report["progress"] = _progress(
        config,
        config_exists=True,
        config_valid=True,
        next_action=next_action,
    )
    return report, next_action


def _status() -> tuple[dict[str, Any], str]:
    report, next_action = _doctor(online=False)
    data = {
        "setup_stage": report["setup_stage"],
        "progress": report["progress"],
        "paths": {"config": report["paths"]["config"]},
        "config": report["config"],
        "queue": report.get("queue", {"pending": 0, "processed": 0, "sync_pending": 0}),
        "warnings": report.get("warnings", []),
    }
    return data, next_action


AGENT_SOURCE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openclaw", ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_GATEWAY_TOKEN")),
    ("hermes", ("HERMES_HOME", "HERMES_STATE_DIR")),
    ("lark-channel", ("LARK_CHANNEL", "LARK_CHANNEL_HOME", "LARK_CHANNEL_APP_ID")),
)


def _detect_agent_source() -> str:
    """Return the hosting Agent platform from its environment signals."""
    for source, names in AGENT_SOURCE_SIGNALS:
        if any(os.environ.get(name) for name in names):
            return source
    return ""


def _feishu_destination(destination: str) -> tuple[dict[str, Any], str]:
    state: dict[str, Any] = {}

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        previous = str(config["feishu"].get("destination") or "undecided")
        config["feishu"]["destination"] = destination
        if destination == "skip":
            config["feishu"]["enabled"] = False
        state["previous"] = previous
        state["changed"] = previous != destination
        if state["changed"]:
            invalidate_policy(config)
        return config

    modify_config(mutate)
    next_action = (
        "review_and_confirm_execution_policy"
        if destination == "skip"
        else "run_feishu_context_then_authorize_only_if_needed"
    )
    return {
        "destination": destination,
        "previous_destination": state["previous"],
        "explicit_user_choice_required": True,
        "target_or_credentials_deleted": False,
        "execution_policy_invalidated": state["changed"],
    }, next_action


def _import_feishu_host_context(
    arguments: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    agent_file = getattr(arguments, "agent_file", None)
    if agent_file is not None:
        raw = Path(agent_file).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            raise ValueError(
                "feishu-host-context --agent-stdin requires trusted host context JSON on stdin"
            )
        raw = sys.stdin.read(16 * 1024 + 1)
    if len(raw.encode("utf-8")) > 16 * 1024:
        raise ValueError("Feishu host context exceeds the input size limit")
    payload = json.loads(raw.lstrip("\ufeff"))
    if not isinstance(payload, dict):
        raise ValueError("Feishu host context must be a JSON object")
    unexpected = set(payload) - {"source", "app_id", "sender_open_id", "sender_id"}
    if unexpected:
        raise ValueError(
            f"Feishu host context contains unsupported keys: {sorted(unexpected)}"
        )
    source = str(payload.get("source") or "").strip().casefold()
    if source not in {"openclaw", "hermes", "lark-channel"}:
        raise ValueError(
            "Feishu host context source must be openclaw, hermes, or lark-channel"
        )
    detected_source = _detect_agent_source()
    if detected_source and detected_source != source:
        raise ValueError(
            "Feishu host context source conflicts with the detected Agent runtime"
        )
    app_id = str(payload.get("app_id") or "").strip()
    if not app_id.startswith("cli_"):
        raise ValueError("trusted Feishu host App ID must start with cli_")
    sender_open_id = str(
        payload.get("sender_open_id") or payload.get("sender_id") or ""
    ).strip()
    if not sender_open_id.startswith("ou_"):
        raise ValueError("trusted Feishu host sender Open ID must start with ou_")

    state: dict[str, Any] = {}

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        destination = config["feishu"]["destination"]
        if destination not in {"existing", "create"}:
            raise ValueError(
                "choose existing or create as the Feishu destination before importing "
                "the current bot context"
            )
        if (
            config["setup"]["feishu_identity_confirmed"]
            and config["feishu"]["identity"] != "bot"
        ):
            raise ValueError(
                "the current setup already confirms user identity; do not silently switch "
                "it to the conversational bot"
            )
        expected_app_id = _expected_app_id(config)
        if expected_app_id and expected_app_id != app_id:
            raise ValueError(
                "the current Feishu conversation App ID conflicts with the saved App ID"
            )
        manager_open_id = str(config["feishu"].get("manager_open_id") or "").strip()
        if manager_open_id and manager_open_id != sender_open_id:
            raise ValueError(
                "the current Feishu sender conflicts with the saved human manager"
            )

        previous_scope = (
            config["feishu"]["identity"],
            config["feishu"]["binding_mode"],
            config["feishu"]["agent_source"],
            config["feishu"]["expected_app_id"],
            config["feishu"]["manager_open_id"],
        )
        config["feishu"].update(
            {
                "identity": "bot",
                "binding_mode": "agent",
                "agent_source": source,
                "expected_app_id": app_id,
                "cli_profile": "",
                "expected_user_open_id": "",
                "manager_open_id": sender_open_id,
            }
        )
        config["setup"]["feishu_identity_confirmed"] = True
        _reset_authorization(config, "bot")
        current_scope = (
            config["feishu"]["identity"],
            config["feishu"]["binding_mode"],
            config["feishu"]["agent_source"],
            config["feishu"]["expected_app_id"],
            config["feishu"]["manager_open_id"],
        )
        state["changed"] = previous_scope != current_scope
        if state["changed"]:
            config["health"]["feishu"] = dict(DEFAULT_CONFIG["health"]["feishu"])
            invalidate_policy(config)
        return config

    modify_config(mutate)
    return {
        "source": source,
        "app_id": app_id,
        "identity": "bot",
        "identity_confirmed": True,
        "manager_configured_from_sender": True,
        "sender_open_id_included": False,
        "binding_mode": "agent",
        "execution_policy_invalidated": state["changed"],
        "host_context_contains_secrets": False,
    }, "bind_detected_feishu_bot"


def _feishu_context(*, verify: bool) -> tuple[dict[str, Any], str]:
    current = load_config()
    if not current["setup"]["feishu_identity_confirmed"]:
        source = _detect_agent_source()
        if source:
            return {
                "identity_required": False,
                "host_bot_context_available": True,
                "agent_source_detected": source,
                "import_command": "manage feishu-host-context --agent-stdin",
                "required_host_fields": ["source", "app_id", "sender_open_id"],
                "rule": (
                    "Read these exact values from the trusted current Feishu host/event "
                    "context. Do not ask the user to type them and do not infer them from "
                    "a display name."
                ),
            }, "import_current_feishu_bot_context"
        return {
            "identity_required": True,
            "choices": {
                "user": (
                    "Use the selected Feishu user's permissions. Reuse a valid existing "
                    "authorization; otherwise start exactly one Base authorization flow."
                ),
                "bot": (
                    "Use app/bot credentials and backend scopes. Never start user authorization."
                ),
            },
            "selection_command": "manage feishu-identity --as user|bot",
        }, "ask_feishu_identity_before_authorization"
    if (
        current["feishu"].get("binding_mode") != "agent"
        and (
            not current["feishu"].get("expected_app_id")
            or not current["feishu"].get("cli_profile")
        )
    ):
        return {
            "identity_required": False,
            "selected_identity": current["feishu"]["identity"],
            "app_selection_required": True,
            "global_profiles_read": False,
            "command": "manage feishu-app --app-id <APP_ID>",
            "rule": (
                "Select the exact App ID first. The Skill creates a private named "
                "profile and never switches or edits global lark-cli profiles."
            ),
        }, "select_feishu_app"
    if current["feishu"].get("binding_mode") == "agent":
        expected_app_id = _expected_app_id(current)
        if not expected_app_id:
            return {
                "identity_required": False,
                "host_bot_context_required": True,
                "global_profiles_read": False,
                "default_profile_allowed": False,
                "command": "manage feishu-host-context --agent-stdin",
                "rule": (
                    "Import the exact App ID from the trusted current Feishu event "
                    "context. Never infer it from the active/default lark-cli profile."
                ),
            }, "import_current_feishu_bot_context"
        profile_resolution = resolve_lark_profile(expected_app_id)
        if current["feishu"].get("cli_profile") != profile_resolution["profile"]:
            def _set_profile(config: dict[str, Any]) -> dict[str, Any]:
                config["feishu"]["cli_profile"] = profile_resolution["profile"]
                return config

            current = modify_config(_set_profile)
        else:
            current = load_config()
    else:
        # Existing/dedicated bindings can also drift from lark-cli's real profile
        # name (e.g. a profile created externally as cli_<app_id>). Resolve by
        # App ID and self-heal when the profile is discoverable; never error when
        # the profile is simply not initialized yet.
        expected_app_id = _expected_app_id(current)
        profile_resolution = None
        if expected_app_id:
            try:
                profile_resolution = resolve_lark_profile(expected_app_id)
            except LarkCLIError:
                profile_resolution = None
        if (
            profile_resolution
            and current["feishu"].get("cli_profile")
            != profile_resolution["profile"]
        ):
            def _set_profile(config: dict[str, Any]) -> dict[str, Any]:
                config["feishu"]["cli_profile"] = profile_resolution["profile"]
                return config

            current = modify_config(_set_profile)
        else:
            current = load_config()
    context = feishu_identity_context(verify=verify)
    source = _detect_agent_source()
    saved_source = str(current["feishu"].get("agent_source") or "")
    selected_identity = str(current["feishu"].get("identity") or "user")
    can_bind = source in {"openclaw", "hermes", "lark-channel"}
    context.update(
        {
            "agent_source_detected": source,
            "agent_source_configured": saved_source,
            "can_bind_current_agent": can_bind,
            "selected_identity": selected_identity,
            "identity_confirmed": True,
            "profile_resolution": profile_resolution,
            "manager_configured": bool(current["feishu"].get("manager_open_id")),
            "selection_rule": (
                "Use the current conversation App ID to select exactly one lark-cli "
                "profile. Never select by default status or bot display name."
            ),
            "binding_modes": {
                "agent": (
                    "Bind the detected Agent (OpenClaw/Hermes/Lark Channel) app after "
                    "explicit confirmation."
                    if can_bind
                    else "Unavailable: this Agent does not expose a supported app binding source."
                ),
                "existing": "Use and explicitly confirm the existing lark-cli App ID/profile.",
                "dedicated": (
                    "Initialize a dedicated Feishu app/profile; recommended for generic "
                    "Agents that cannot prove the conversational bot identity."
                ),
            },
        }
    )
    if not context["app_id_unambiguous"]:
        return context, "select_or_initialize_feishu_profile"
    selected = context[selected_identity]
    ready = bool(selected["available"]) and selected["status"] == "ready"
    if selected_identity == "user":
        ready = ready and selected.get("token_status") in {"", "valid"}
        if not ready:
            if _authorization(current)["state"] == "waiting":
                return context, "resume_existing_user_base_authorization"
            return context, "run_feishu_auth_start"
        return context, "reuse_existing_user_authorization_and_confirm_context"
    if not ready:
        return context, "configure_bot_credentials_and_scopes_without_user_auth"
    if not current["feishu"].get("manager_open_id"):
        return context, "resolve_and_save_feishu_manager"
    return context, "confirm_feishu_app_and_bot"


def _feishu_identity(identity: str) -> dict[str, Any]:
    state: dict[str, Any] = {}

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        previous = str(config["feishu"].get("identity") or "user")
        was_confirmed = bool(config["setup"]["feishu_identity_confirmed"])
        config["feishu"]["identity"] = identity
        config["setup"]["feishu_identity_confirmed"] = True
        state["previous"] = previous
        state["changed"] = previous != identity or not was_confirmed
        if state["changed"]:
            config["health"]["feishu"] = dict(DEFAULT_CONFIG["health"]["feishu"])
            _reset_authorization(config, identity)
            invalidate_policy(config)
        return config

    config = modify_config(mutate)
    return {
        "identity": identity,
        "previous_identity": state["previous"],
        "identity_confirmed": True,
        "authorization_policy": (
            "reuse an existing valid user authorization; otherwise start one Base authorization flow"
            if identity == "user"
            else "use bot credentials and backend scopes; never start user authorization"
        ),
        "authorization": dict(_authorization(config)),
    }


def _feishu_app(app_id: str) -> dict[str, Any]:
    normalized = app_id.strip()
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", normalized):
        raise ValueError("Feishu App ID must start with cli_ and contain only letters/digits")
    profile = profile_name_for_app(normalized)

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        if not config["setup"]["feishu_identity_confirmed"]:
            raise ValueError("select user or bot identity before selecting the Feishu app")
        previous = str(config["feishu"].get("expected_app_id") or "")
        config["feishu"]["expected_app_id"] = normalized
        config["feishu"]["cli_profile"] = profile
        if not config["feishu"].get("binding_mode"):
            config["feishu"]["binding_mode"] = "existing"
        if previous != normalized:
            config["health"]["feishu"] = dict(DEFAULT_CONFIG["health"]["feishu"])
            _reset_authorization(config, config["feishu"]["identity"])
            invalidate_policy(config)
            config["feishu"].update(
                {
                    "enabled": False,
                    "expected_user_open_id": "",
                    "manager_open_id": "",
                    "base_token": "",
                    "table_id": "",
                    "provisioning": "",
                    "field_mapping": {},
                }
            )
        return config

    modify_config(mutate)
    return {
        "app_selected": True,
        "app_id_included": False,
        "private_profile": profile,
        "global_profiles_modified": False,
        "next_command": (
            "lark config init --app-id <CONFIRMED_APP_ID> "
            "--app-secret-stdin"
        ),
        "profile_name_added_automatically": True,
    }


def _feishu_setup() -> tuple[dict[str, Any], str]:
    """Dialogue-ready Feishu onboarding state: what to ask, what to run next.

    A fresh user needs no app information prepared: this command reports the
    current stage, the question to put to the user, and the exact next
    command, including console guidance for creating a new app.
    """
    config = load_config()
    feishu = config["feishu"]
    app_id = _expected_app_id(config)
    state: dict[str, Any] = {
        "identity_confirmed": bool(config["setup"]["feishu_identity_confirmed"]),
        "identity": feishu["identity"],
        "app_bound": bool(app_id),
        "app_id": app_id,
        "profile": feishu["cli_profile"],
        "authorization": _authorization(config)["state"],
        "destination": feishu["destination"],
        "target_configured": bool(feishu["base_token"] and feishu["table_id"]),
    }
    guide = {
        "create_app_url": "https://open.feishu.cn/app?lang=zh-CN",
        "create_app_steps": [
            "在飞书开放平台创建一个企业自建应用（名字随意，例如 文章订阅同步）。",
            "在应用后台 权限管理 开通多维表格权限（base:app、base:table、base:record、base:field 的读写）。",
            "发布应用版本，然后从 凭证与基础信息 复制 App ID 和 App Secret。",
        ],
    }
    if not state["identity_confirmed"]:
        state.update(
            next_question="飞书用哪种身份写入：个人用户（扫码授权一次）还是机器人应用？",
            next_command="manage feishu-identity --as user|bot",
        )
        return state, "ask_feishu_identity_before_authorization"
    if not state["app_bound"]:
        state.update(
            next_question="请提供飞书应用的 App ID（或按引导去开放平台创建一个新应用）。",
            next_command="manage feishu-app --app-id <APP_ID>",
            create_app_guide=guide,
        )
        return state, "select_feishu_app"
    if not state["profile"]:
        state.update(
            next_question="确认将该应用导入技能的私有配置？",
            next_command="manage feishu-local-profile import --yes",
        )
        return state, "reuse_or_configure_private_lark_profile"
    if state["authorization"] != "authorized":
        state.update(
            next_question="应用已绑定但密钥/授权未就绪：请提供 App Secret（stdin），随后完成一次扫码授权。",
            next_command=(
                f"printf %s '<APP_SECRET>' | manage feishu-app-secret --app-id {app_id}"
            ),
            then="manage feishu-auth start（扫码后 feishu-auth complete）",
            create_app_guide=guide,
        )
        return state, "provide_app_secret_for_private_profile"
    if state["destination"] == "undecided":
        state.update(
            next_question="文章写入飞书的哪里：跳过、映射现有表格、还是创建新 Base？",
            next_command="manage feishu-destination --mode skip|existing|create",
        )
        return state, "ask_user_for_feishu_destination"
    if not state["target_configured"] and state["destination"] == "create":
        state.update(
            next_question="确认创建标准文章表？",
            next_command="manage feishu-create-base --name 公众号文章 --table-name 文章列表",
        )
        return state, "provision_configured_feishu_base"
    state.update(
        next_question=None,
        next_command="manage doctor --online（最终校验）",
    )
    return state, "run_feishu_validation"


def _feishu_app_secret(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Pipe one App Secret from stdin into the isolated lark-cli profile."""
    from bitable_client import _run_lark, probe_app_secret_resolution

    config = load_config()
    app_id = _expected_app_id(config)
    if not app_id:
        raise ConfigError("bind the App ID first with manage feishu-app")
    if arguments.app_id and arguments.app_id.strip() != app_id:
        raise ValueError(
            f"--app-id {arguments.app_id} does not match the confirmed App ID {app_id}"
        )
    if not config["setup"]["feishu_identity_confirmed"]:
        raise ConfigError("confirm Feishu identity before entering an App Secret")
    secret = _read_secret_stdin("the Feishu App Secret")
    if not secret:
        raise ValueError("the App Secret is empty")
    _run_lark(
        ["config", "init", "--app-id", app_id, "--app-secret-stdin"],
        retries=1,
        input_text=secret,
    )
    probe = probe_app_secret_resolution()
    return {
        "app_id": app_id,
        "secret_accepted": probe["resolvable"],
        "probe": probe,
    }, (
        "run_feishu_context_then_authorize_only_if_needed"
        if probe["resolvable"]
        else "provide_app_secret_for_private_profile"
    )


def _feishu_local_profile(
    arguments: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    """Inspect or import one existing user-level lark-cli app safely."""
    inventory = discover_global_lark_profiles()
    if arguments.local_profile_command == "scan":
        try:
            config = load_config()
        except ConfigError:
            expected_app_id = ""
            private_profile = ""
        else:
            expected_app_id = _expected_app_id(config)
            private_profile = str(config["feishu"].get("cli_profile") or "").strip()
        matching = [
            item
            for item in inventory["profiles"]
            if item["app_id"] == expected_app_id
        ]
        return {
            **inventory,
            "selected_app_id": expected_app_id,
            "private_profile": private_profile,
            "selected_match_count": len(matching),
            "read_only": True,
            "original_config_modified": False,
        }, (
            "select_feishu_app"
            if not expected_app_id
            else (
                "reuse_or_configure_private_lark_profile"
                if len(matching) == 1
                else "configure_private_lark_profile"
            )
        )

    config = load_config()
    if not config["setup"]["feishu_identity_confirmed"]:
        raise ConfigError("confirm Feishu identity before importing a local profile")
    expected_app_id = _expected_app_id(config)
    private_profile = str(config["feishu"].get("cli_profile") or "").strip()
    if not expected_app_id or not private_profile:
        raise ConfigError(
            "select the exact App ID with manage feishu-app before importing a local profile"
        )
    matching = [
        item for item in inventory["profiles"] if item["app_id"] == expected_app_id
    ]
    if len(matching) != 1:
        raise ConfigError(
            f"expected exactly one existing local profile for App ID {expected_app_id}; "
            f"found {len(matching)}"
        )
    selected = matching[0]
    if not selected["app_secret_available"]:
        raise ConfigError(
            "the selected local profile has no reusable App credential; configure the "
            "isolated profile through secret stdin"
        )
    if not arguments.yes:
        return {
            "preview": {
                "source_config": inventory["path"],
                "source_profile": selected["name"],
                "app_id": expected_app_id,
                "target_private_profile": private_profile,
                "app_secret_storage": selected["app_secret_storage"],
                "copies_app_credential": True,
                "copies_user_tokens": False,
                "modifies_original_config": False,
                "secret_values_displayed": False,
            }
        }, "rerun_with_yes"
    result = import_global_lark_profile(expected_app_id, private_profile)
    # An import can clone the keychain *reference* while the isolated home
    # cannot decrypt it; prove the secret works before telling the user the
    # profile is ready, and surface the console-copy remediation when not.
    probe = probe_app_secret_resolution()
    result["app_secret_resolvable"] = probe["resolvable"]
    if not probe["resolvable"]:
        result["app_secret_remediation"] = probe.get("remediation") or probe.get("message")
    return result, (
        "run_feishu_context_then_authorize_only_if_needed"
        if probe["resolvable"]
        else "provide_app_secret_for_private_profile"
    )


def _feishu_grant_manager(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    config = load_config()
    if not config["setup"]["feishu_identity_confirmed"]:
        raise LarkCLIError(
            "confirm bot identity before creating or sharing a Feishu resource",
            kind="wrong_app",
        )
    if config["feishu"]["identity"] != "bot":
        raise LarkCLIError(
            "automatic manager provisioning applies only to bot-created resources",
            kind="config",
        )
    manager_open_id = str(config["feishu"].get("manager_open_id") or "").strip()
    if not manager_open_id:
        raise LarkCLIError(
            "no human manager is configured. Resolve the invoking user's exact open_id "
            "and save it as feishu.manager_open_id before bot provisioning.",
            kind="config",
        )
    verify_feishu_identity(config["feishu"], identity="bot")
    # Resource tokens are sensitive and must not appear in shell history or
    # the manage process argv. The official lark-cli still receives the token
    # in its required --token argument inside the wrapper.
    resource_token = sys.stdin.read().strip()
    if not resource_token:
        raise ValueError("resource token is required on stdin")
    grant_bot_created_resource(resource_token, arguments.resource_type, manager_open_id)
    return {
        "resource_type": arguments.resource_type,
        "permission": "full_access",
        "manager_granted": True,
        "manager_open_id_included": False,
        "identity": "bot",
    }, "continue_resource_provisioning"


def _feishu_create_base(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    config = load_config()
    if config["feishu"]["destination"] != "create":
        raise LarkCLIError(
            "Feishu Base creation requires the explicit destination=create choice",
            kind="confirmation",
        )
    has_token = bool(str(config["feishu"].get("base_token") or "").strip())
    has_table = bool(str(config["feishu"].get("table_id") or "").strip())
    resuming = (
        config["feishu"].get("provisioning") == "created"
        and has_token
        and has_table
    )
    schema = standard_field_schema()
    base_name = " ".join(str(arguments.name).split())
    table_name = " ".join(str(arguments.table_name).split())
    preview = {
        "base_name": base_name,
        "table_name": table_name,
        "identity": config["feishu"]["identity"],
        "field_count": len(schema),
        "field_names": [field["name"] for field in schema],
        "transport": "native lark-cli binary with an argv array; no shell JSON",
        "global_profiles_modified": False,
        "resuming_existing_base": resuming,
    }
    policy_authorized = allows_automatic_provisioning(
        config,
        base_name=base_name,
        table_name=table_name,
    )
    preview["authorization_source"] = (
        "persisted_execution_policy" if policy_authorized else "current_command"
    )
    if not arguments.yes and not policy_authorized:
        return {
            "preview": preview,
            "created": False,
            "policy_match": False,
        }, "rerun_with_yes"
    if (has_token or has_table) and not resuming:
        raise LarkCLIError(
            "a Feishu target is already configured; refusing to create another Base "
            "without a new target decision",
            kind="config",
        )
    if resuming:
        stored_base_name = str(
            config["feishu"].get("created_base_name") or ""
        ).strip()
        stored_table_name = str(
            config["feishu"].get("created_table_name") or ""
        ).strip()
        if stored_base_name and base_name != stored_base_name:
            raise LarkCLIError(
                f"the earlier Base was created as {stored_base_name!r}; rerun with "
                "the same --name to resume it",
                kind="config",
            )
        if stored_table_name and table_name != stored_table_name:
            raise LarkCLIError(
                f"the earlier Base table was created as {stored_table_name!r}; "
                "rerun with the same --table-name to resume it",
                kind="config",
            )
    if not config["setup"]["feishu_identity_confirmed"]:
        raise LarkCLIError("confirm Feishu identity before Base creation", kind="config")
    identity = config["feishu"]["identity"]
    if (
        not config["feishu"].get("cli_profile")
        and config["feishu"].get("binding_mode") != "agent"
    ):
        raise LarkCLIError(
            "select the Skill-owned Feishu app/profile before Base creation",
            kind="config",
        )
    manager_open_id = str(config["feishu"].get("manager_open_id") or "").strip()
    if identity == "bot" and not manager_open_id:
        raise LarkCLIError(
            "configure the invoking user as manager before bot Base creation",
            kind="config",
        )
    verify_feishu_identity(config["feishu"], identity=identity)
    if resuming:
        base_token = str(config["feishu"]["base_token"])
        table_id = str(config["feishu"]["table_id"])
    else:
        payload = create_standard_base(
            base_name,
            table_name,
            identity=identity,
        )
        base_token, table_id = created_base_identifiers(payload)

    # Persist the recovery anchor before any external permission/schema step,
    # so a later failure can resume from this exact state.
    def mutate_created(config: dict[str, Any]) -> dict[str, Any]:
        config["feishu"].update(
            {
                "enabled": False,
                "base_token": base_token,
                "table_id": table_id,
                "provisioning": "created",
                "field_mapping": {},
                "created_base_name": str(
                    config["feishu"].get("created_base_name") or ""
                ).strip()
                or base_name,
                "created_table_name": str(
                    config["feishu"].get("created_table_name") or ""
                ).strip()
                or table_name,
            }
        )
        return config

    config = modify_config(mutate_created)
    manager_granted = identity != "bot"
    if identity == "bot":
        try:
            grant_bot_created_resource(base_token, "bitable", manager_open_id)
        except LarkCLIError as exc:
            if not resuming or exc.kind != "duplicate":
                raise
            # Re-running after a partial grant reports the member as already
            # present (classified as kind="duplicate"); treat that as success.
        manager_granted = True

    check = preflight_feishu(config["feishu"])

    def mutate_complete(config: dict[str, Any]) -> dict[str, Any]:
        config["feishu"].update(
            {
                "enabled": True,
                "field_mapping": check["mapping"],
            }
        )
        config["setup"]["execution_policy"]["allow_feishu_provisioning"] = False
        config["setup"]["execution_policy"]["provision_base_name"] = ""
        config["setup"]["execution_policy"]["provision_table_name"] = ""
        return config

    config = modify_config(mutate_complete)
    update_health("feishu", success=True)
    return {
        "created": True,
        **preview,
        "base_token": base_token,
        "table_id": table_id,
        "manager_granted": manager_granted,
        "field_mapping_saved": True,
        "resumed_existing": resuming,
        "provisioning_approval_consumed": policy_authorized,
        "authorization_source": (
            "persisted_execution_policy" if policy_authorized else "current_command"
        ),
    }, "none"


def _feishu_manager(open_id: str) -> dict[str, Any]:
    normalized = open_id.strip()
    if not normalized.startswith("ou_"):
        raise ValueError("manager Open ID must start with ou_")

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        if not config["setup"]["feishu_identity_confirmed"] or config["feishu"]["identity"] != "bot":
            raise ValueError("select and confirm bot identity before setting its human manager")
        previous = str(config["feishu"].get("manager_open_id") or "")
        config["feishu"]["manager_open_id"] = normalized
        if previous != normalized:
            invalidate_policy(config)
        return config

    modify_config(mutate)
    return {
        "manager_configured": True,
        "manager_open_id_included": False,
        "permission_for_new_bot_resources": "full_access",
    }


def _execution_policy_command(
    arguments: argparse.Namespace,
) -> tuple[dict[str, Any], str]:
    config = load_config()
    if arguments.policy_command == "show":
        return {
            "policy": deepcopy(policy_for(config)),
            "allowed_when_confirmed": [
                "routine discovery, reading, scoring, queueing, and export",
                "exact-name standard Feishu Base provisioning when enabled",
                "qualified record sync to the configured Feishu target when enabled",
            ],
            "always_requires_new_authorization": [
                "OAuth/device-page completion or new scopes",
                "App, identity, manager, target, or schema changes",
                "forced below-threshold Feishu writes",
                "delete, reset, and other destructive actions",
            ],
        }, "none"

    base_name = (arguments.base_name or "").strip()
    table_name = (arguments.table_name or "").strip()
    provisioning_allowed = arguments.feishu_provisioning == "allow"
    sync_allowed = arguments.feishu_sync == "allow"
    destination = config["feishu"]["destination"]
    if destination == "undecided":
        raise ValueError(
            "choose the Feishu destination before previewing the execution policy"
        )
    if destination == "skip" and (provisioning_allowed or sync_allowed):
        raise ValueError(
            "Feishu provisioning and sync must both be denied when destination=skip"
        )
    if destination == "existing" and provisioning_allowed:
        raise ValueError(
            "Feishu provisioning cannot be allowed when destination=existing"
        )
    if arguments.mode == "guided" and (provisioning_allowed or sync_allowed):
        raise ValueError("guided mode requires both Feishu permissions=deny")
    if provisioning_allowed and (not base_name or not table_name):
        raise ValueError(
            "--base-name and --table-name are required when Feishu provisioning is allowed"
        )
    if not provisioning_allowed and (base_name or table_name):
        raise ValueError(
            "--base-name/--table-name are only valid when Feishu provisioning is allowed"
        )
    proposed = {
        **deepcopy(DEFAULT_CONFIG["setup"]["execution_policy"]),
        "confirmed": True,
        "mode": arguments.mode,
        "allow_feishu_provisioning": provisioning_allowed,
        "provision_base_name": base_name,
        "provision_table_name": table_name,
        "allow_feishu_sync": sync_allowed,
        "approved_at": "",
    }
    preview = {
        "feishu_destination": destination,
        "policy": proposed,
        "effect": (
            "After this one confirmation, the Agent continues automatically inside "
            "this exact scope without asking again."
        ),
        "excluded": [
            "new OAuth scopes or completing the user-owned authorization page",
            "changes to the Feishu App, identity, manager, target, or schema",
            "forced below-threshold writes",
            "delete, reset, and other destructive actions",
        ],
    }
    if not arguments.yes:
        return {"preview": preview, "saved": False}, "rerun_with_yes"
    proposed["approved_at"] = datetime.now(timezone.utc).isoformat()

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        config["setup"]["execution_policy"] = proposed
        return config

    modify_config(mutate)
    return {
        "saved": True,
        "policy": deepcopy(proposed),
        "agent_may_continue": arguments.mode == "autopilot",
        "additional_routine_confirmations_required": False,
        "excluded": preview["excluded"],
    }, "continue_setup_then_execute"


def _identity_ready(context: dict[str, Any], identity: str) -> bool:
    selected = context.get(identity)
    if not isinstance(selected, dict):
        return False
    ready = bool(selected.get("available")) and selected.get("status") == "ready"
    if identity == "user":
        ready = ready and selected.get("token_status") in {"", "valid"}
    return ready


def _save_authorization_state(
    state: str,
    *,
    started: bool = False,
    completed: bool = False,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        authorization = _authorization(config)
        authorization["state"] = state
        authorization["identity"] = config["feishu"]["identity"]
        authorization["updated_at"] = now
        if started:
            authorization["started_at"] = now
        if completed:
            authorization["completed_at"] = now
        if state in {"waiting", "expired", "failed", "not_started"}:
            authorization["completed_at"] = ""
        return config

    config = modify_config(mutate)
    return dict(_authorization(config))


def _feishu_auth(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    config = load_config()
    if not config["setup"]["feishu_identity_confirmed"]:
        return {
            "identity_confirmed": False,
            "authorization": dict(_authorization(config)),
        }, "ask_feishu_identity_before_authorization"
    identity = config["feishu"]["identity"]
    authorization = _authorization(config)
    if arguments.auth_command == "status":
        return {
            "identity": identity,
            "authorization": dict(authorization),
            "secrets_included": False,
        }, (
            "resume_existing_user_base_authorization"
            if authorization["state"] == "waiting"
            else "none"
        )
    if arguments.auth_command == "expire":
        if not arguments.yes:
            return {
                "preview": "mark the current user authorization flow expired",
                "authorization": dict(authorization),
            }, "rerun_with_yes"
        return {
            "identity": identity,
            "authorization": _save_authorization_state("expired"),
        }, "run_feishu_auth_start"
    if identity == "bot":
        state = _save_authorization_state("not_required", completed=True)
        return {
            "identity": "bot",
            "authorization": state,
            "user_authorization_started": False,
        }, "configure_bot_credentials_and_scopes_without_user_auth"
    if arguments.auth_command == "start":
        if authorization["state"] == "waiting":
            return {
                "identity": identity,
                "authorization": dict(authorization),
                "new_authorization_started": False,
            }, "resume_existing_user_base_authorization"
        context = feishu_identity_context(verify=True)
        if context.get("app_id_unambiguous") is False:
            return {
                "identity": identity,
                "authorization": dict(authorization),
                "new_authorization_started": False,
            }, "select_or_initialize_feishu_profile"
        if _identity_ready(context, "user"):
            state = _save_authorization_state("authorized", completed=True)
            return {
                "identity": identity,
                "authorization": state,
                "new_authorization_started": False,
                "existing_authorization_reused": True,
            }, "confirm_feishu_app_and_user"
        state = _save_authorization_state("waiting", started=True)
        return {
            "identity": identity,
            "authorization": state,
            "new_authorization_started": True,
            "authorization_command": "lark auth login --domain base --no-wait --json",
            "device_code_persisted": False,
        }, "start_single_user_base_authorization"
    context = feishu_identity_context(verify=True)
    if context.get("app_id_unambiguous") is False:
        return {
            "identity": identity,
            "authorization": dict(authorization),
            "authorization_verified": False,
        }, "select_or_initialize_feishu_profile"
    if _identity_ready(context, "user"):
        state = _save_authorization_state("authorized", completed=True)
        return {
            "identity": identity,
            "authorization": state,
            "authorization_verified": True,
        }, "confirm_feishu_app_and_user"
    if authorization["state"] != "waiting":
        return {
            "identity": identity,
            "authorization": dict(authorization),
            "authorization_verified": False,
            "new_authorization_started": False,
        }, "run_feishu_auth_start"
    state = dict(authorization)
    return {
        "identity": identity,
        "authorization": state,
        "authorization_verified": False,
        "new_authorization_started": False,
    }, "finish_existing_user_base_authorization"


def _daily(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Preview the full daily plan for confirmation, then run it with --yes."""
    from article_inbox import plan_digest
    from discover_only import discover_articles

    config = load_config()
    subscriptions = [
        {
            "name": str(item.get("name", "")).strip(),
            "alias": str(item.get("alias", "")).strip(),
            "cooldown_active": bool(item.get("last_discovered_at")),
        }
        for item in config["subscriptions"]
    ]
    feishu = config["feishu"]
    plan: dict[str, Any] = {
        "subscriptions": subscriptions,
        "window_hours": config["settings"]["check_hours"],
        "min_score": config["settings"]["min_score"],
        "digest": {
            "hours": config["preferences"]["digest_hours"],
            "limit": config["preferences"]["digest_limit"],
            "include_topics": config["preferences"]["include_topics"],
            "exclude_keywords": config["preferences"]["exclude_keywords"],
        },
        "feishu": {
            "enabled": feishu["enabled"],
            "destination": feishu["destination"],
            "target_configured": bool(feishu["base_token"] and feishu["table_id"]),
            "sync_allowed_by_policy": bool(
                config["setup"]["execution_policy"]["allow_feishu_sync"]
            ),
            "mapped_fields": sorted(feishu["field_mapping"]),
        },
        "estimated_billed_calls": sum(
            1 for item in subscriptions if not item["cooldown_active"]
        ),
        "note": "1 list call per subscription outside its cooldown, plus 1 detail call per article read",
    }
    if not arguments.yes:
        return plan, "confirm_daily_run"

    diagnostics: list[dict] = []
    queued = 0

    def persist(articles: list[dict]) -> int:
        nonlocal queued
        from queue_helpers import add_pending

        added = add_pending(
            articles, content_dedup=bool(config["settings"]["content_dedup"])
        )
        queued += added
        return added

    discovered = discover_articles(
        config, float(config["settings"]["check_hours"]), None, diagnostics, persist
    )
    preferences = config["preferences"]
    digest = plan_digest(preferences, hours=preferences["digest_hours"], limit=preferences["digest_limit"])
    plan["run"] = {
        "discovered": len(discovered),
        "queued": queued,
        "accounts": diagnostics,
        "digest_candidates": digest["candidates"],
    }
    return plan, "read_score_digest_candidates"


def _resolve_alias_by_name(name: str) -> str:
    """Resolve one display name to its WeChat alias via one paid search call."""
    from redfox_client import RedfoxClient

    api_key = load_config()["redfox"]["api_key"].strip()
    if not api_key:
        raise ConfigError("redfox API key is missing; run the redfox key setup command")
    client = RedfoxClient(api_key)
    try:
        candidates = client.search_accounts(name)
    finally:
        client.close()
    exact = [c for c in candidates if c["account_name"] == name]
    if len(exact) == 1:
        return exact[0]["account"]
    if not exact:
        raise ValueError(
            f"no account named {name!r} in the redfox library; check the name or "
            "supply the WeChat alias directly with --alias (free)"
        )
    listing = ", ".join(f"{c['account_name']}/{c['account']}" for c in exact)
    raise ValueError(
        f"multiple accounts named {name!r}: {listing}; pick one and rerun with "
        "--alias <WECHAT_ALIAS>"
    )


def _subscriptions(arguments: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    items = config["subscriptions"]
    if arguments.subscription_command == "list":
        query = str(arguments.query or "").strip().casefold()
        selected = [
            item
            for item in items
            if not query
            or query
            in " ".join(str(item.get(key, "")) for key in ("name", "alias", "biz")).casefold()
        ]
        return {"subscriptions": selected, "count": len(selected), "total": len(items)}
    if arguments.subscription_command == "add":
        candidate = {
            key: value.strip()
            for key, value in {"name": arguments.name, "alias": arguments.alias, "biz": arguments.biz}.items()
            if value and value.strip()
        }
        if not candidate:
            raise ValueError("provide --name and/or --alias (--biz alone is not queryable)")
        if not candidate.get("alias"):
            # Two supported input modes, cheapest first:
            # 1. the user supplies the WeChat alias directly (free, exact);
            # 2. only a display name was given -> one paid search-call to
            #    resolve it. Ambiguous names are reported, never guessed.
            if not candidate.get("name"):
                raise ValueError("--biz alone cannot be discovered; provide --name or --alias")
            resolved = _resolve_alias_by_name(candidate["name"])
            candidate["alias"] = resolved
        identity = {str(candidate.get(key, "")).casefold() for key in ("name", "alias", "biz") if candidate.get(key)}
        state: dict[str, Any] = {}

        def mutate_add(config: dict[str, Any]) -> dict[str, Any]:
            current_items = config["subscriptions"]
            for existing in current_items:
                existing_identity = {
                    str(existing.get(key, "")).casefold()
                    for key in ("name", "alias", "biz")
                    if existing.get(key)
                }
                if identity & existing_identity:
                    raise ValueError("subscription already exists")
            current_items.append(candidate)
            state["count"] = len(current_items)
            return config

        modify_config(mutate_add)
        return {"added": candidate, "count": state["count"]}
    if arguments.subscription_command == "bulk-add":
        candidates: list[Any] = list(arguments.name or [])
        if arguments.file:
            try:
                raw = arguments.file.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise ValueError(f"cannot read subscription file: {exc}") from exc
            if arguments.file.suffix.casefold() == ".json":
                try:
                    loaded = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"subscription JSON is invalid: {exc}") from exc
                if not isinstance(loaded, list):
                    raise ValueError("subscription JSON must be an array")
                candidates.extend(loaded)
            else:
                candidates.extend(
                    line.strip()
                    for line in raw.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                )
        if not candidates:
            raise ValueError("provide one or more --name values or --file")
        if len(candidates) > 100:
            raise ValueError("cannot add more than 100 subscriptions at once")
        def normalize(value: Any) -> dict[str, str]:
            if isinstance(value, str):
                return {"name": value.strip()}
            if isinstance(value, dict):
                unexpected = set(value) - {"name", "alias", "biz"}
                if unexpected:
                    raise ValueError(f"unsupported subscription keys: {sorted(unexpected)}")
                normalized = {}
                for key in ("name", "alias", "biz"):
                    raw = value.get(key, "")
                    if not isinstance(raw, str):
                        raise ValueError(f"subscription {key} must be a string")
                    if raw.strip():
                        normalized[key] = raw.strip()
                return normalized
            raise ValueError("each subscription must be a name or object")

        normalized_candidates = [normalize(value) for value in candidates]
        state: dict[str, Any] = {}

        def mutate_bulk(config: dict[str, Any]) -> dict[str, Any]:
            current_items = config["subscriptions"]
            existing_identities = {
                str(item.get(key, "")).strip().casefold()
                for item in current_items
                for key in ("name", "alias", "biz")
                if str(item.get(key, "")).strip()
            }
            added_local: list[dict[str, str]] = []
            skipped_local: list[str] = []
            for candidate in normalized_candidates:
                identities = {item.casefold() for item in candidate.values() if item}
                if not identities:
                    raise ValueError("subscription entries cannot be empty")
                if identities & existing_identities:
                    skipped_local.append(candidate.get("name") or next(iter(candidate.values())))
                    continue
                added_local.append(candidate)
                existing_identities.update(identities)
            state["added"] = added_local
            state["skipped"] = skipped_local
            state["total"] = len(current_items) + len(added_local)
            current_items.extend(added_local)
            return config

        if not arguments.dry_run:
            modify_config(mutate_bulk)
        else:
            existing_identities = {
                str(item.get(key, "")).strip().casefold()
                for item in items
                for key in ("name", "alias", "biz")
                if str(item.get(key, "")).strip()
            }
            state["added"] = []
            state["skipped"] = []
            for candidate in normalized_candidates:
                identities = {item.casefold() for item in candidate.values() if item}
                if not identities:
                    raise ValueError("subscription entries cannot be empty")
                if identities & existing_identities:
                    state["skipped"].append(candidate.get("name") or next(iter(candidate.values())))
                else:
                    state["added"].append(candidate)
                    existing_identities.update(identities)
        return {
            "dry_run": bool(arguments.dry_run),
            "added": state["added"],
            "added_count": len(state["added"]),
            "skipped_duplicates": state["skipped"],
            "total": (
                len(items) + len(state["added"])
                if arguments.dry_run
                else state["total"]
            ),
        }
    selector = arguments.value.casefold()
    state: dict[str, Any] = {}

    def mutate_remove(config: dict[str, Any]) -> dict[str, Any]:
        current_items = config["subscriptions"]
        retained = [
            item
            for item in current_items
            if selector
            not in {
                str(item.get(key, "")).casefold() for key in ("name", "alias", "biz")
            }
        ]
        removed = len(current_items) - len(retained)
        if not removed:
            raise LookupError("subscription not found")
        config["subscriptions"] = retained
        state["removed"] = removed
        state["count"] = len(retained)
        return config

    modify_config(mutate_remove)
    return {"removed": state["removed"], "count": state["count"]}


def _preferences(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    config = load_config()
    current = config["preferences"]
    if arguments.preference_command == "show":
        return {"preferences": current}, "none"
    if arguments.preference_command == "clear":
        if not arguments.yes:
            return {
                "preview": dict(DEFAULT_CONFIG["preferences"]),
                "current": current,
            }, "rerun_with_yes"

        def mutate_clear(config: dict[str, Any]) -> dict[str, Any]:
            config["preferences"] = dict(DEFAULT_CONFIG["preferences"])
            return config

        saved = modify_config(mutate_clear)
        return {"preferences": saved["preferences"], "cleared": True}, "none"
    updates: dict[str, Any] = {}
    list_updates = {
        "include_topics": arguments.include_topic,
        "exclude_keywords": arguments.exclude_keyword,
        "preferred_accounts": arguments.preferred_account,
    }
    for key, values in list_updates.items():
        if values is not None:
            cleaned = list(
                dict.fromkeys(" ".join(value.split()) for value in values if value.strip())
            )
            updates[key] = cleaned
    if arguments.digest_hours is not None:
        updates["digest_hours"] = arguments.digest_hours
    if arguments.digest_limit is not None:
        updates["digest_limit"] = arguments.digest_limit
    if not updates:
        raise ValueError("provide at least one preference update")

    def mutate_update(config: dict[str, Any]) -> dict[str, Any]:
        config["preferences"].update(updates)
        return config

    saved = modify_config(mutate_update)
    return {"preferences": saved["preferences"], "updated_fields": sorted(updates)}, "generate_digest_plan"


def _key_tail(api_key: str) -> str:
    """Last four characters only; empty for keys too short to be identifiable."""
    return api_key[-4:] if len(api_key) >= 8 else ""


def _read_secret_stdin(name: str) -> str:
    """Read a bounded secret from piped stdin, never from an interactive TTY."""
    if sys.stdin.isatty():
        raise ValueError(f"{name} must be piped on standard input, not typed interactively")
    value = sys.stdin.read(64 * 1024 + 1)
    if len(value) > 64 * 1024:
        raise ValueError(f"{name} exceeds 64 KiB")
    return value.strip()


def _probe_redfox(api_key: str) -> dict[str, Any]:
    """One paid reachability probe; classifies failures via the protocol."""
    from redfox_client import RedfoxAuthError, RedfoxClient

    client = RedfoxClient(api_key)
    try:
        # The probe only proves the key authenticates and the service answers;
        # a code=0/3203 response with an empty list is still a pass.
        client.query_work_list(account="probe", offset=0, count=1)
        return {"reachable": True}
    except RedfoxAuthError:
        return {"reachable": False, "error_code": "REDFOX_AUTH"}
    except Exception as exc:  # classified by the protocol layer
        return {"reachable": False, "error_code": getattr(exc, "code", type(exc).__name__)}
    finally:
        client.close()


# First-contact commands a fresh user may run before any configuration
# exists. They seed the default configuration instead of failing with
# "configuration not found"; read-only diagnostics (status/doctor/config-show)
# and reset intentionally stay out so they keep reporting the true state.
SEED_CONFIG_COMMANDS = frozenset(
    {
        "redfox-set-key",
        "subscriptions",
        "preferences",
        "execution-policy",
        "feishu-identity",
        "feishu-destination",
        "feishu-app",
        "feishu-app-secret",
        "feishu-local-profile",
        "feishu-host-context",
        "feishu-manager",
    }
)


def _seed_config_if_missing() -> None:
    if not config_path().exists():
        save_config(validate_config(dict(DEFAULT_CONFIG)))


def _redfox_set_key() -> tuple[dict[str, Any], str]:
    api_key = _read_secret_stdin("the redfox API key")
    if not api_key:
        raise ValueError("the redfox API key is empty")
    _seed_config_if_missing()

    def mutate_key(config: dict[str, Any]) -> dict[str, Any]:
        config["redfox"]["api_key"] = api_key
        return config

    saved = modify_config(mutate_key)
    return {
        "configured": bool(saved["redfox"]["api_key"].strip()),
        "key_tail": _key_tail(api_key),
    }, "discover_articles"


def _redfox_status(*, verify: bool = False) -> tuple[dict[str, Any], str]:
    config = load_config()
    api_key = config["redfox"]["api_key"].strip()
    data: dict[str, Any] = {
        "configured": bool(api_key),
        "key_tail": _key_tail(api_key),
    }
    if api_key and verify:
        # One paid call; only on explicit request.
        data.update(_probe_redfox(api_key))
    elif api_key:
        data["reachable"] = None  # not checked; pass --verify for a live probe
    else:
        data["reachable"] = False
    return data, "none"


def _reset(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    scope = arguments.scope
    targets: list[Path] = []
    if scope in {"queue", "all-data"}:
        targets.extend([queue_path(), lock_path()])
    if scope == "all-data":
        root = config_path().parent
        targets.extend(
            [
                config_path(),
                root / "config.lock",
                root / "queue.lock",
                root / "fields.json",
            ]
        )
        for pattern in (
            "config.v*.backup.json",
            ".agent-config-*.json",
            "feishu-auth-qr*.png",
            "queue.corrupt.*.json",
            ".config.json.*",
            ".queue.json.*",
        ):
            targets.extend(root.glob(pattern))
        targets.extend(
            [
                root / "lark-cli-config",
                root / "lark-cli-home",
                root / "lark-cli-work",
            ]
        )
        # Keep the reset allowlist-based. Unknown files may belong to the user,
        # especially when WECHAT_ARTICLE_HOME points at a portable directory.
    existing = sorted({path.resolve() for path in targets if path.exists()}, key=str)
    if not arguments.yes:
        return {"preview": [str(path) for path in existing], "deleted": []}, "rerun_with_yes"
    if scope == "credentials":
        def mutate_reset(config: dict[str, Any]) -> dict[str, Any]:
            config["redfox"] = {"api_key": ""}
            config["setup"]["feishu_identity_confirmed"] = False
            config["setup"]["feishu_authorization"] = dict(
                DEFAULT_CONFIG["setup"]["feishu_authorization"]
            )
            config["setup"]["execution_policy"] = deepcopy(
                DEFAULT_CONFIG["setup"]["execution_policy"]
            )
            config["feishu"].update({
                "destination": "undecided",
                "enabled": False,
                "binding_mode": "",
                "agent_source": "",
                "expected_app_id": "",
                "cli_profile": "",
                "expected_user_open_id": "",
                "manager_open_id": "",
                "base_token": "",
                "table_id": "",
                "field_mapping": {},
                "provisioning": "",
            })
            config["health"] = validate_config(DEFAULT_CONFIG)["health"]
            return config

        modify_config(mutate_reset)
        return {"cleared": "credentials", "preserved": ["subscriptions", "settings", "queue"]}, "ask_user_to_choose_chat_or_local_file"
    root = data_dir().resolve()
    for target in existing:
        if target.parent != root and target not in {
            (root / "lark-cli-config").resolve(),
            (root / "lark-cli-home").resolve(),
            (root / "lark-cli-work").resolve(),
        }:
            raise ValueError(f"refusing to delete state outside the application directory: {target}")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    return {"deleted": [str(path) for path in existing], "recoverable": False}, "none"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument(
        "--online",
        action="store_true",
        help="include live checks; the redfox probe makes 1 billed API call",
    )
    commands.add_parser("status")
    commands.add_parser("feishu-setup")
    app_secret = commands.add_parser("feishu-app-secret")
    app_secret.add_argument("--app-id", default="", help="optional; must match the confirmed App ID")
    daily = commands.add_parser("daily")
    daily.add_argument(
        "--yes",
        action="store_true",
        help="run the confirmed plan: discover, then produce the digest candidates",
    )
    # The redfox key is always read from piped stdin; there is no flag so no
    # caller can believe it may pass the secret as an argument.
    commands.add_parser(
        "redfox-set-key",
        description="read the redfox API key from piped standard input",
    )
    redfox_status = commands.add_parser("redfox-status")
    redfox_status.add_argument(
        "--verify",
        action="store_true",
        help="make one paid API call to verify connectivity",
    )
    commands.add_parser("config-show")
    policy = commands.add_parser("execution-policy")
    policy_commands = policy.add_subparsers(dest="policy_command", required=True)
    policy_commands.add_parser("show")
    set_policy = policy_commands.add_parser("set")
    set_policy.add_argument("--mode", choices=("guided", "autopilot"), required=True)
    set_policy.add_argument(
        "--feishu-provisioning",
        choices=("allow", "deny"),
        required=True,
    )
    set_policy.add_argument("--base-name")
    set_policy.add_argument("--table-name")
    set_policy.add_argument(
        "--feishu-sync",
        choices=("allow", "deny"),
        required=True,
    )
    set_policy.add_argument("--yes", action="store_true")
    destination = commands.add_parser("feishu-destination")
    destination.add_argument(
        "--mode",
        choices=("skip", "existing", "create"),
        required=True,
    )
    host_context = commands.add_parser("feishu-host-context")
    host_sources = host_context.add_mutually_exclusive_group(required=True)
    host_sources.add_argument(
        "--agent-stdin", action="store_true", help="read host context JSON from stdin"
    )
    host_sources.add_argument(
        "--agent-file",
        type=Path,
        help="read trusted host context JSON from a UTF-8 file (Windows-safe)",
    )
    context = commands.add_parser("feishu-context")
    context.add_argument("--verify", action="store_true")
    identity = commands.add_parser("feishu-identity")
    identity.add_argument("--as", dest="identity", choices=("user", "bot"), required=True)
    app = commands.add_parser("feishu-app")
    app.add_argument("--app-id", required=True)
    local_profile = commands.add_parser("feishu-local-profile")
    local_profile_commands = local_profile.add_subparsers(
        dest="local_profile_command", required=True
    )
    local_profile_commands.add_parser("scan")
    import_profile = local_profile_commands.add_parser("import")
    import_profile.add_argument("--yes", action="store_true")
    manager = commands.add_parser("feishu-manager")
    manager.add_argument("--open-id", required=True)
    grant_manager = commands.add_parser("feishu-grant-manager")
    grant_manager.add_argument(
        "--token-stdin",
        action="store_true",
        required=True,
        help="read the resource token from stdin; never pass it as a command-line value",
    )
    grant_manager.add_argument(
        "--type",
        dest="resource_type",
        choices=("bitable", "doc", "docx", "file", "folder", "sheet", "slides", "wiki"),
        required=True,
    )
    create_base = commands.add_parser("feishu-create-base")
    create_base.add_argument("--name", required=True)
    create_base.add_argument("--table-name", required=True)
    create_base.add_argument("--yes", action="store_true")
    auth = commands.add_parser("feishu-auth")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_commands.add_parser("status")
    auth_commands.add_parser("start")
    auth_commands.add_parser("complete")
    expire = auth_commands.add_parser("expire")
    expire.add_argument("--yes", action="store_true")
    subs = commands.add_parser("subscriptions")
    subcommands = subs.add_subparsers(dest="subscription_command", required=True)
    list_subscriptions = subcommands.add_parser("list")
    list_subscriptions.add_argument("--query", default="")
    add = subcommands.add_parser("add")
    add.add_argument("--name", default="")
    add.add_argument(
        "--alias",
        default="",
        help="WeChat alias (微信号): free and exact; preferred. Phone WeChat shows it on the account profile page.",
    )
    add.add_argument("--biz", default="", help="kept for identity matching only; not queryable")
    bulk_add = subcommands.add_parser("bulk-add")
    bulk_add.add_argument("--name", action="append", default=[])
    bulk_add.add_argument("--file", type=Path)
    bulk_add.add_argument("--dry-run", action="store_true")
    remove = subcommands.add_parser("remove")
    remove.add_argument("value")
    preferences = commands.add_parser("preferences")
    preference_commands = preferences.add_subparsers(
        dest="preference_command", required=True
    )
    preference_commands.add_parser("show")
    set_preferences = preference_commands.add_parser("set")
    set_preferences.add_argument("--include-topic", action="append")
    set_preferences.add_argument("--exclude-keyword", action="append")
    set_preferences.add_argument("--preferred-account", action="append")
    set_preferences.add_argument("--digest-hours", type=int)
    set_preferences.add_argument("--digest-limit", type=int)
    clear_preferences = preference_commands.add_parser("clear")
    clear_preferences.add_argument("--yes", action="store_true")
    disable = commands.add_parser("feishu-disable")
    disable.add_argument("--yes", action="store_true")
    reset = commands.add_parser("reset")
    reset.add_argument("--scope", choices=("credentials", "queue", "all-data"), required=True)
    reset.add_argument("--yes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command in SEED_CONFIG_COMMANDS:
            _seed_config_if_missing()
        next_action = "none"
        if arguments.command == "doctor":
            data, next_action = _doctor(online=arguments.online)
        elif arguments.command == "status":
            data, next_action = _status()
        elif arguments.command == "feishu-setup":
            data, next_action = _feishu_setup()
        elif arguments.command == "feishu-app-secret":
            data, next_action = _feishu_app_secret(arguments)
        elif arguments.command == "daily":
            data, next_action = _daily(arguments)
        elif arguments.command == "redfox-set-key":
            data, next_action = _redfox_set_key()
        elif arguments.command == "redfox-status":
            data, next_action = _redfox_status(verify=arguments.verify)
        elif arguments.command == "config-show":
            data = redacted_config(load_config())
        elif arguments.command == "execution-policy":
            data, next_action = _execution_policy_command(arguments)
        elif arguments.command == "feishu-destination":
            data, next_action = _feishu_destination(arguments.mode)
        elif arguments.command == "feishu-host-context":
            data, next_action = _import_feishu_host_context(arguments)
        elif arguments.command == "feishu-context":
            data, next_action = _feishu_context(verify=arguments.verify)
        elif arguments.command == "feishu-identity":
            data = _feishu_identity(arguments.identity)
            next_action = "run_feishu_context_then_authorize_only_if_needed"
        elif arguments.command == "feishu-app":
            data = _feishu_app(arguments.app_id)
            next_action = "reuse_or_configure_private_lark_profile"
        elif arguments.command == "feishu-local-profile":
            data, next_action = _feishu_local_profile(arguments)
        elif arguments.command == "feishu-manager":
            data = _feishu_manager(arguments.open_id)
            next_action = "confirm_feishu_app_and_bot"
        elif arguments.command == "feishu-grant-manager":
            data, next_action = _feishu_grant_manager(arguments)
        elif arguments.command == "feishu-create-base":
            data, next_action = _feishu_create_base(arguments)
        elif arguments.command == "feishu-auth":
            data, next_action = _feishu_auth(arguments)
        elif arguments.command == "subscriptions":
            data = _subscriptions(arguments)
            if arguments.subscription_command == "add":
                next_action = "discover_articles"
            elif arguments.subscription_command == "bulk-add" and data["added_count"]:
                next_action = (
                    "review_and_apply_subscription_batch"
                    if arguments.dry_run
                    else "discover_articles"
                )
        elif arguments.command == "preferences":
            data, next_action = _preferences(arguments)
        elif arguments.command == "feishu-disable":
            if not arguments.yes:
                data, next_action = {"preview": "disable Feishu sync; no Base data is deleted"}, "rerun_with_yes"
            else:
                def mutate_disable(config: dict[str, Any]) -> dict[str, Any]:
                    config["feishu"]["enabled"] = False
                    config["setup"]["execution_policy"]["allow_feishu_sync"] = False
                    return config

                modify_config(mutate_disable)
                data = {"disabled": True, "base_data_deleted": False}
        else:
            data, next_action = _reset(arguments)
        envelope = success(data, next_action=next_action)
        print(dump(envelope) if arguments.format == "json" else json.dumps(envelope, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        envelope = failure(exc)
        print(dump(envelope) if arguments.format == "json" else json.dumps(envelope, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
