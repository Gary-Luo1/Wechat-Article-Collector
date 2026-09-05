#!/usr/bin/env python3
"""Inspect, patch, diagnose, and safely reset skill state."""

from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import platform
import shutil
import sys
from pathlib import Path
from typing import Any
from article_inbox import queue_summary

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
    next_stage,
    policy_for,
    stage_facts,
)
from lark_runtime import LarkCLIError, lark_cli_info

from feishu_target import production_feishu_target
from paths import config_path, data_dir, lock_path, queue_path, venv_dir
from protocol import (
    NEXT_ACTIONS,
    _pipe_cmd,
    _read_secret_stdin,
    dump,
    failure,
    hoist_format_flag,
    success,
)
from manage_feishu import (
    _authorized_user_open_id,
    _feishu_app,
    _feishu_app_secret,
    _feishu_auth,
    _feishu_context,
    _feishu_create_base,
    _feishu_destination,
    _feishu_grant_manager,
    _feishu_identity,
    _feishu_local_profile,
    _feishu_manager,
    _feishu_setup,
    _feishu_target,
    _import_feishu_host_context,
)


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
    "run_doctor_online": "在线体检 redfox API 与飞书",
    "run_online_doctor": "在线体检 redfox API 与飞书",
    "reuse_or_configure_private_lark_profile": "复用或初始化技能私有的 lark-cli 配置",
    "rerun_feishu_create_base_to_resume": "用相同名称重跑 feishu-create-base 以续接未完成的建表",
    "configure_existing_feishu_target": "提供目标表格链接以映射现有表格",
    "inspect_failed_items": "检查失败条目",
    "process_pending_articles": "处理待读文章",
    "edit_then_validate_local_config": "编辑并校验本地配置文件",
    "run_redfox_key_setup": "通过 stdin 设置 redfox API Key",
    "confirm_daily_run": "确认以上计划后加 --yes 执行每日发现与简报",
    "collect_redfox_key": "提供 redfox API Key（stdin 或对话内提供皆可）",
    "rerun_with_yes": "预览无误后加 --yes 执行",
    "run_feishu_validation": "运行飞书只读校验",
    "select_or_initialize_feishu_profile": "选择或初始化技能私有的 lark-cli 配置",
    "reuse_existing_user_authorization_and_confirm_context": "复用现有个人授权并确认上下文",
    "configure_bot_credentials_and_scopes_without_user_auth": "检查机器人凭据与权限（无需扫码）",
    "confirm_feishu_app_and_bot": "确认飞书应用与机器人身份",
    "confirm_feishu_app_and_user": "确认飞书应用与个人身份",
    "start_single_user_base_authorization": "发起一次最小权限扫码授权",
    "finish_existing_user_base_authorization": "完成等待中的扫码授权",
    "continue_resource_provisioning": "继续被批准的资源创建",
    "read_score_digest_candidates": "阅读并评分简报候选文章",
    "generate_digest_plan": "生成文章简报计划",
    "treat_as_already_done": "目标已存在，视为完成",
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
    "ask_user_for_feishu_setup_choice": "本地未发现 lark-cli 或应用配置：询问用户如何继续（安装 / 提供应用信息 / 跳过飞书），不扩大检索范围",
    "install_compatible_lark_cli": "安装兼容的飞书 CLI 版本",
    "authorize_and_run_feishu_check": "完成飞书只读检查",
    "resolve_and_save_feishu_manager": "确认接收机器人文件管理权限的飞书用户",
    "select_feishu_app": "选择并固定本技能要使用的飞书 App ID",
    "configure_private_lark_profile": "在技能私有目录中配置已选飞书应用",
    "provide_app_secret_for_private_profile": "从飞书开放平台复制 App Secret 并经 stdin 初始化私有配置",
    "provision_configured_feishu_base": "自动创建并验证已批准的飞书多维表格",
    "continue_setup_then_execute": "继续完成配置并自动执行任务",
    "discover_articles": "发现并查看新文章",
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
        facts = stage_facts(config)
        checks.extend(
            [
                ("redfox_credentials", facts["redfox_key"], False),
                ("search_window", facts["search_window"], False),
                ("subscriptions", facts["subscriptions"], False),
                (
                    "subscription_resolution",
                    facts["subscriptions"] and facts["subscriptions_resolved"],
                    False,
                ),
                (
                    "feishu_destination",
                    facts["feishu_destination"] != "undecided",
                    False,
                ),
                ("execution_policy", facts["policy_confirmed"], False),
            ]
        )
        feishu_requested = facts["feishu_destination"] in {"existing", "create"}
        if feishu_requested:
            authorization_ready = (
                facts["feishu_identity"] == "bot"
                or facts["authorization_state"] in {"authorized", "not_required"}
            )
            feishu_ready = not facts["feishu_unverified"] and not facts["feishu_failed"]
            checks.extend(
                [
                    ("feishu_identity", facts["feishu_identity_confirmed"], False),
                    ("feishu_authorization", authorization_ready, False),
                    ("feishu_target", facts["feishu_target_configured"], False),
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
                "error": {
                    "code": "REDFOX_AUTH",
                    "message": "redfox API key is missing; run redfox-set-key",
                    "retryable": False,
                    "next_action": "run_redfox_key_setup",
                },
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
                online_report["redfox"] = {"ok": False, "error": failure(exc)["error"]}
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
                online_report["feishu"] = {"ok": False, "error": failure(exc)["error"]}
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


def _daily(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Preview the full daily plan for confirmation, then run it with --yes."""
    from article_inbox import plan_digest
    from discover_only import discover_articles

    config = load_config()
    from discover_only import _subscription_cooldown_active

    interval = float(config["settings"]["check_hours"])
    subscriptions = [
        {
            "name": str(item.get("name", "")).strip(),
            "alias": str(item.get("alias", "")).strip(),
            "cooldown_active": _subscription_cooldown_active(item, interval),
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
            1
            for item in subscriptions
            if item["alias"] and not item["cooldown_active"]
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


def _next_step() -> tuple[dict[str, Any], str]:
    """One universal dialogue step: what to ask the user, what to run next.

    The Agent loops on this command (plus `manage feishu-setup` inside the
    Feishu branch) until it reports ready, so every configuration decision is
    made in dialogue instead of by handing the user a command list.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        if config_path().exists():
            return {
                "stage": "config_invalid",
                "question": f"本地配置损坏：{exc}。需要修复或重置（manage reset 可预览）。",
                "command": "manage doctor",
                "paid": False,
            }, "repair_local_config_file"
        return {
            "stage": "fresh_install",
            "question": "请提供 redfox API key（在 https://redfox.hk/ 控制台创建；也可自己执行 printf 管道命令以避免聊天留存）",
            "command": _pipe_cmd("printf %s '<KEY>' | manage redfox-set-key"),
            "paid": False,
        }, "collect_redfox_key"

    if not config["redfox"]["api_key"].strip():
        return {
            "stage": "redfox_key_missing",
            "question": "redfox API key 缺失：请在 https://redfox.hk/ 控制台创建后提供。",
            "command": _pipe_cmd("printf %s '<KEY>' | manage redfox-set-key"),
            "paid": False,
        }, "collect_redfox_key"

    cli: dict[str, Any] | None = None
    try:
        cli = lark_cli_info()
    except LarkCLIError:
        cli = None
    stage, action = next_stage(config, cli=cli)
    questions: dict[str, tuple[str, str, bool]] = {
        "search_window_unconfirmed": (
            "每次拉多久以内的文章？24 小时（推荐）/ 48 小时 / 7 天 / 自定义",
            _pipe_cmd("printf %s '{\"check_hours\":24}' | setup --agent-stdin --section settings"),
            False,
        ),
        "subscriptions_missing": (
            "想订阅哪些公众号？直接报名称或微信号（微信号可在公众号手机主页查看；只报名称会花 1 次解析调用）",
            "manage subscriptions add --name <名称> [--alias <微信号>]",
            "名称模式 1 次调用/个",
        ),
        "subscriptions_unresolved": (
            "有订阅缺少微信号（数据源只认微信号）：为现有订阅补 alias 用 set-alias，新订阅直接带 alias 添加。",
            "manage subscriptions set-alias --name <名称或旧alias> --alias <微信号>",
            False,
        ),
        "feishu_destination_unconfirmed": (
            "要把文章同步到飞书多维表格吗？跳过 / 写入已有表格 / 新建标准表格 —— 由 feishu-setup 引导（含字段确认与身份选择）",
            "manage feishu-setup",
            False,
        ),
        "feishu_identity_unconfirmed": ("请选择飞书执行身份（个人扫码 / 机器人免扫码）。", "manage feishu-setup", False),
        "feishu_cli_missing_or_unchecked": (
            "本地没有可用的 lark-cli：需要先安装（Node.js + npm install @larksuite/cli 到技能隔离目录）。是否现在安装？或先跳过飞书（manage feishu-destination --mode skip）。",
            "manage feishu-setup",
            False,
        ),
        "feishu_cli_incompatible": ("lark-cli 版本不兼容，需要安装受支持版本。", "manage feishu-setup", False),
        "feishu_secret_missing": (
            "bot 身份需要应用的 App Secret（stdin 管道提供，不经过聊天回显）：请从开放平台应用的『凭证与基础信息』复制后交给 Agent。",
            _pipe_cmd("printf %s '<APP_SECRET>' | manage feishu-app-secret --app-id <APP_ID>"),
            False,
        ),
        "feishu_authorization_required": ("需要一次飞书扫码授权（最小权限）。",
            "manage feishu-setup", False),
        "feishu_authorization_waiting": ("上一次扫码授权还在等待：请完成页面确认或重新发起。",
            "manage feishu-setup", False),
        "feishu_manager_missing": (
            "bot 模式需要一位接收管理权限的飞书用户：优先用曾授权的个人身份导入（免输入），否则提供 Open ID。",
            "manage feishu-manager --from-authorized-user（或 --open-id <OPEN_ID>）",
            False,
        ),
        "feishu_target_pending": (
            "已批准建表：先固化执行策略（含相同 Base/表名，预览后 --yes 生效），再运行 create-base——策略同名精确匹配即自动授权，勿加 --yes。",
            "manage execution-policy set --mode autopilot --feishu-provisioning allow --base-name <已批准名> --table-name <已批准名> --feishu-sync <allow|deny> [--yes] → manage feishu-create-base --name <已批准名> --table-name <已批准名>",
            False,
        ),
        "feishu_target_missing": ("请提供目标表格：飞书表格链接（manage feishu-target --url <链接>）或按引导新建。",
            "manage feishu-setup", False),
        "feishu_validation_failed": ("飞书只读校验失败：按 feishu-setup 指引修复。", "manage feishu-setup", False),
        "feishu_unverified": ("飞书配置完成后需要一次只读校验。", "process feishu-check --save-mapping", False),
        "execution_policy_unconfirmed": (
            "最后一步：一次性确认自动执行范围（发现/读取/评分 + 是否允许自动同步飞书）。预览后加 --yes 生效。",
            "manage execution-policy set --mode autopilot --feishu-provisioning <allow|deny> --feishu-sync <allow|deny> [--base-name X --table-name Y] [--yes]",
            False,
        ),
    }
    if stage in {"ready_wechat_only", "ready"}:
        return {
            "stage": stage,
            "question": None,
            "command": "manage daily  （先预览，用户确认后 --yes 执行）",
            "paid": "预览免费；执行按订阅数计费",
            "ready": True,
        }, "confirm_daily_run"
    question, command, paid = questions.get(
        stage, (None, "manage status", False)
    )
    return {
        "stage": stage,
        "question": question or f"按诊断处理：{stage}",
        "command": command,
        "paid": paid,
        "ready": False,
    }, action


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
    if arguments.subscription_command == "set-alias":
        alias = str(arguments.alias or "").strip()
        if not alias:
            raise ValueError("--alias is required")
        selector = str(arguments.name or "").strip()

        def mutate_alias(config: dict[str, Any]) -> dict[str, Any]:
            matches = [
                item
                for item in config["subscriptions"]
                if selector
                and selector.casefold()
                in (
                    str(item.get("name", "")).strip().casefold(),
                    str(item.get("alias", "")).strip().casefold(),
                )
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"expected exactly one subscription matching {selector!r}; "
                    f"found {len(matches)}"
                )
            # Same identity rule as add/bulk-add: a duplicate alias would be
            # billed twice per cycle and make later selectors ambiguous.
            for item in config["subscriptions"]:
                if item is matches[0]:
                    continue
                taken = {
                    str(item.get(key, "")).strip().casefold()
                    for key in ("name", "alias", "biz")
                }
                if alias.casefold() in taken:
                    raise ValueError(
                        f"alias {alias!r} is already used by subscription "
                        f"{str(item.get('name') or item.get('alias'))!r}"
                    )
            matches[0]["alias"] = alias
            return config

        saved = modify_config(mutate_alias)
        return {"subscriptions": saved["subscriptions"]}

    if arguments.subscription_command == "add":
        _pending_resolution_billing = not (arguments.alias or "").strip()
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
        result = {"added": candidate, "count": state["count"]}
        if _pending_resolution_billing:
            result["billed_calls"] = 1
            result["billed_note"] = "1 paid account-search call was used to resolve the name"
        return result
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
            raise ValueError("subscription not found")
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


def _probe_redfox(api_key: str) -> dict[str, Any]:
    """One paid reachability probe; classifies failures via the protocol."""
    from redfox_client import RedfoxAuthError, RedfoxClient

    client = RedfoxClient(api_key)
    try:
        # The probe only proves the key authenticates and the service answers;
        # a code=0/3203 response with an empty list is still a pass.
        client.query_work_list(account="probe", offset=0, count=1)
        return {"reachable": True}
    except RedfoxAuthError as exc:
        return {
            "reachable": False,
            "error_code": "REDFOX_AUTH",
            "message": str(exc)[:200],
        }
    except Exception as exc:  # classified by the protocol layer
        return {
            "reachable": False,
            "error_code": getattr(exc, "code", type(exc).__name__),
            "message": str(exc)[:200],
        }
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
        data["billed"] = 1
    elif api_key:
        data["reachable"] = None  # not checked; pass --verify for a live probe
    else:
        data["reachable"] = False
    return data, "none"


def _credentials_reset_preview(config: dict[str, Any]) -> list[str]:
    """List the configured values a credentials reset would clear.

    The credentials scope mutates config fields instead of deleting files, so
    the preview must describe those fields or it would misleadingly show
    "nothing to do" right before wiping the Feishu binding and API key.
    """
    entries: list[str] = []
    if config["redfox"]["api_key"].strip():
        entries.append("redfox.api_key")
    if config["setup"]["feishu_identity_confirmed"]:
        entries.append("setup.feishu_identity_confirmed")
    if (
        config["setup"]["feishu_authorization"].get("state")
        != DEFAULT_CONFIG["setup"]["feishu_authorization"]["state"]
    ):
        entries.append("setup.feishu_authorization")
    policy = config["setup"]["execution_policy"]
    if (
        policy.get("confirmed")
        or policy.get("allow_feishu_provisioning")
        or policy.get("allow_feishu_sync")
    ):
        entries.append("setup.execution_policy")
    for field in (
        "agent_source",
        "binding_mode",
        "expected_app_id",
        "cli_profile",
        "expected_user_open_id",
        "manager_open_id",
        "base_token",
        "table_id",
        "provisioning",
    ):
        if str(config["feishu"].get(field) or "").strip():
            entries.append(f"feishu.{field}")
    if config["feishu"].get("destination") != "undecided":
        entries.append("feishu.destination")
    if config["feishu"].get("field_mapping"):
        entries.append("feishu.field_mapping")
    return entries


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
        preview: list[str] = [str(path) for path in existing]
        if scope == "credentials":
            preview = _credentials_reset_preview(load_config())
        return {"preview": preview, "deleted": []}, "rerun_with_yes"
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
    feishu_target = commands.add_parser("feishu-target")
    feishu_target.add_argument("--url", required=True, help="Feishu base URL of the exact table")
    commands.add_parser("next")
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
    manager.add_argument("--open-id", default="")
    manager.add_argument(
        "--from-authorized-user",
        action="store_true",
        help="import the Open ID of the previously authorized personal-identity user",
    )
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
    set_alias = subcommands.add_parser("set-alias")
    set_alias.add_argument("--name", default="", help="subscription name or current alias")
    set_alias.add_argument("--alias", default="", help="the WeChat alias to store")
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


def _failed_online_envelope(data: dict[str, Any]) -> dict[str, Any] | None:
    """Build an ok:false envelope when any doctor --online check failed.

    SKILL.md requires the setup flow to "report connectivity problems and
    stop"; a failed online check must not hide behind a top-level ok:true.
    """
    online = data.get("online")
    if not isinstance(online, dict):
        return None
    for section, entry in online.items():
        if isinstance(entry, dict) and entry.get("ok") is False:
            error = dict(entry.get("error") or {})
            error.setdefault("code", "INTERNAL_ERROR")
            error.setdefault("message", f"online check failed: {section}")
            error.setdefault("retryable", False)
            error.setdefault("next_action", "inspect_command_help")
            return {"ok": False, "data": data, "error": error}
    return None


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(argv if argv is not None else sys.argv[1:])
    arguments = build_parser().parse_args(hoist_format_flag(raw_arguments))
    try:
        if arguments.command in SEED_CONFIG_COMMANDS:
            _seed_config_if_missing()
        next_action = "none"
        if arguments.command == "doctor":
            data, next_action = _doctor(online=arguments.online)
            if arguments.online:
                failed = _failed_online_envelope(data)
                if failed is not None:
                    print(dump(failed) if arguments.format == "json" else json.dumps(failed, ensure_ascii=False, indent=2))
                    return 1
        elif arguments.command == "status":
            data, next_action = _status()
        elif arguments.command == "feishu-setup":
            data, next_action = _feishu_setup()
        elif arguments.command == "feishu-target":
            data, next_action = _feishu_target(arguments)
        elif arguments.command == "next":
            data, next_action = _next_step()
        elif arguments.command == "feishu-app-secret":
            data, next_action = _feishu_app_secret(arguments)
        elif arguments.command == "daily":
            data, next_action = _daily(arguments)
        elif arguments.command == "redfox-set-key":
            data, next_action = _redfox_set_key()
        elif arguments.command == "redfox-status":
            data, next_action = _redfox_status(verify=arguments.verify)
            if arguments.verify and data.get("reachable") is False:
                code = str(data.get("error_code") or "INTERNAL_ERROR")
                envelope = {
                    "ok": False,
                    "data": data,
                    "error": {
                        "code": code,
                        "message": str(
                            data.get("message") or "redfox reachability check failed"
                        )[:500],
                        "retryable": code in {"REDFOX_RATE_LIMITED", "REDFOX_TRANSIENT"},
                        "next_action": NEXT_ACTIONS.get(code, "inspect_command_help"),
                    },
                }
                print(dump(envelope) if arguments.format == "json" else json.dumps(envelope, ensure_ascii=False, indent=2))
                return 1
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
            open_id = arguments.open_id or ""
            if arguments.from_authorized_user:
                open_id = _authorized_user_open_id()
                if not open_id:
                    raise ValueError(
                        "no authorized personal-identity user found; run a user "
                        "authorization first or pass --open-id"
                    )
            data = _feishu_manager(open_id)
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
        elif arguments.command == "reset":
            data, next_action = _reset(arguments)
        else:
            # New parser commands must be wired explicitly; falling through to
            # a destructive preview by default would hide dispatch mistakes.
            raise ValueError(f"unhandled manage command: {arguments.command}")
        envelope = success(data, next_action=next_action)
        print(dump(envelope) if arguments.format == "json" else json.dumps(envelope, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        envelope = failure(exc)
        print(dump(envelope) if arguments.format == "json" else json.dumps(envelope, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())