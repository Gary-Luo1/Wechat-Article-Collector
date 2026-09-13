"""Deep Feishu setup journey: state transitions and next actions.

The command adapter parses arguments and renders envelopes.  This module owns
the setup state machine so every entry point applies the same invalidation and
resume rules.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from config_transitions import transition
from lark_profile_store import profile_name_for_app

AGENT_SOURCE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openclaw", ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_GATEWAY_TOKEN")),
    ("hermes", ("HERMES_HOME", "HERMES_STATE_DIR")),
    ("lark-channel", ("LARK_CHANNEL", "LARK_CHANNEL_HOME", "LARK_CHANNEL_APP_ID")),
)


def stage_facts(
    config: dict[str, Any],
    *,
    cli: dict[str, Any] | None = None,
    profile_secret: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate setup gates once for progress, wizard, and next-action views."""
    feishu = config["feishu"]
    policy = config["setup"]["execution_policy"]
    if profile_secret is None and isinstance(cli, dict):
        profile_secret = cli.get("profile_secret")
    return {
        "redfox_key": bool(config["redfox"]["api_key"].strip()),
        "search_window": bool(config["setup"]["search_window_confirmed"]),
        "subscriptions": bool(config["subscriptions"]),
        "subscriptions_resolved": all(
            str(item.get("alias", "")).strip() for item in config["subscriptions"]
        ),
        "feishu_destination": str(feishu["destination"]),
        "policy_confirmed": bool(policy["confirmed"]),
        "feishu_identity_confirmed": bool(config["setup"]["feishu_identity_confirmed"]),
        "feishu_identity": str(feishu["identity"]),
        "app_bound": bool(str(feishu.get("expected_app_id") or "").strip()),
        "cli_checked": cli is not None,
        "cli_compatible": bool(cli.get("compatible")) if isinstance(cli, dict) else False,
        "bot_secret_missing": (
            feishu["identity"] == "bot"
            and bool(str(feishu.get("expected_app_id") or "").strip())
            and str(feishu.get("binding_mode") or "") != "agent"
            and isinstance(profile_secret, dict)
            and profile_secret.get("ready") is False
        ),
        "authorization_state": str(config["setup"]["feishu_authorization"]["state"]),
        "bot_manager_missing": feishu["identity"] == "bot" and not feishu["manager_open_id"],
        "feishu_target_configured": bool(feishu["base_token"] and feishu["table_id"]),
        "provision_incomplete": feishu.get("provisioning") == "created" and not feishu["enabled"],
        "feishu_failed": bool(config["health"]["feishu"]["consecutive_failures"]),
        "feishu_unverified": not bool(config["health"]["feishu"]["last_verified_at"]),
    }


def detect_agent_source() -> str:
    """Return the hosting Agent platform from its environment signals."""
    for source, names in AGENT_SOURCE_SIGNALS:
        if any(os.environ.get(name) for name in names):
            return source
    return ""


def choose_destination(destination: str) -> tuple[dict[str, Any], str]:
    """Persist a Feishu destination decision and return its next action."""
    if destination not in {"skip", "existing", "create"}:
        raise ValueError("destination must be skip, existing, or create")
    state = transition("feishu_destination", destination)
    return {
        "destination": destination,
        "previous_destination": state["previous"],
        "explicit_user_choice_required": True,
        "target_or_credentials_deleted": False,
        "execution_policy_invalidated": state["changed"],
    }, (
        "review_and_confirm_execution_policy"
        if destination == "skip"
        else "run_feishu_context_then_authorize_only_if_needed"
    )


def bind_agent_context(source: str, app_id: str, sender_open_id: str) -> tuple[dict[str, Any], str]:
    """Bind trusted host context to the Feishu setup scope."""
    state = transition(
        "feishu_agent_context",
        {"source": source, "app_id": app_id, "sender_open_id": sender_open_id},
    )
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


def choose_identity(identity: str) -> dict[str, Any]:
    """Persist the identity decision and clear stale Feishu health/auth state."""
    if identity not in {"user", "bot"}:
        raise ValueError("identity must be user or bot")
    state = transition("feishu_identity", identity)
    return {
        "identity": identity,
        "previous_identity": state["previous"],
        "identity_confirmed": True,
        "authorization_policy": (
            "reuse an existing valid user authorization; otherwise start one Base authorization flow"
            if identity == "user"
            else "use bot credentials and backend scopes; never start user authorization"
        ),
        "authorization": state["authorization"],
    }


def choose_app(app_id: str) -> dict[str, Any]:
    """Persist the selected App ID and reset state tied to an older App."""
    normalized = app_id.strip()
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", normalized):
        raise ValueError("Feishu App ID must start with cli_ and contain only letters/digits")
    profile = profile_name_for_app(normalized)
    transition("feishu_app", {"app_id": normalized, "profile": profile})
    return {
        "app_selected": True,
        "app_id_included": False,
        "private_profile": profile,
        "global_profiles_modified": False,
        "next_command": "",
        "profile_name_added_automatically": True,
    }


def set_manager(open_id: str) -> dict[str, Any]:
    """Persist the bot's human manager and invalidate stale provisioning approval."""
    normalized = open_id.strip()
    if not normalized.startswith("ou_"):
        raise ValueError("manager Open ID must start with ou_")
    transition("feishu_manager", normalized)
    return {
        "manager_configured": True,
        "manager_open_id_included": False,
        "permission_for_new_bot_resources": "full_access",
    }


def save_authorization_state(
    state: str,
    *,
    started: bool = False,
    completed: bool = False,
) -> dict[str, Any]:
    """Persist one authorization transition atomically."""
    return transition(
        "feishu_authorization",
        {"state": state, "started": started, "completed": completed},
    )["authorization"]


def setup_status(
    config: dict[str, Any],
    *,
    secret_state: dict[str, Any],
    secret_command: str,
    field_names: list[str],
    authorized_user: Callable[[], str],
) -> tuple[dict[str, Any], str]:
    """Return the dialogue-ready Feishu onboarding state and next action."""
    feishu = config["feishu"]
    facts = stage_facts(config, profile_secret=secret_state)
    state: dict[str, Any] = {
        "identity_confirmed": facts["feishu_identity_confirmed"],
        "identity": facts["feishu_identity"],
        "app_bound": facts["app_bound"],
        "app_id": str(feishu.get("expected_app_id") or "").strip(),
        "profile": feishu["cli_profile"],
        "authorization": facts["authorization_state"],
        "destination": facts["feishu_destination"],
        "target_configured": facts["feishu_target_configured"],
    }
    guide = {
        "create_app_url": "https://open.feishu.cn/app?lang=zh-CN",
        "create_app_steps": [
            "个人账号需先拥有一个飞书团队/企业（免费创建即可），然后在开放平台创建企业自建应用。",
            "在 权限管理 搜索并勾选多维表格相关权限（控制台以中文名展示，例如「查看、评论、编辑和管理多维表格」及其子项，覆盖表格/字段/记录的读写）。",
            "在 可用范围 里把自己加入应用可用人员，否则授权与写入会被拒绝。",
            "发布应用版本；发布后从 凭证与基础信息 复制 App ID 和 App Secret。",
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
    state["profile_secret_ready"] = secret_state["ready"]
    if facts["bot_secret_missing"]:
        state.update(
            next_question=(
                "bot 身份需要应用的 App Secret 才能调用飞书 API：Agent 会创建并打开"
                "一个本地密钥文件，把开放平台应用『凭证与基础信息』里的 App Secret "
                "粘贴进去保存即可（不经过聊天，也不需要运行命令）。"
            ),
            next_command=secret_command,
            create_app_guide=guide,
        )
        return state, "provide_app_secret_for_private_profile"
    if facts["bot_manager_missing"]:
        try:
            known_user = authorized_user()
        except Exception:
            known_user = ""
        state.update(
            next_question=(
                "bot 身份不需要扫码授权。需要一位接收管理权限的飞书用户："
                + (
                    f"检测到曾授权的用户（{known_user[:12]}…），可直接采用。"
                    if known_user
                    else "请提供接收人的飞书 Open ID（个人版可在开放平台应用的『用户 ID 查询』工具获取）。"
                )
            ),
            next_command=(
                "manage feishu-manager --from-authorized-user"
                if known_user
                else "manage feishu-manager --open-id <OPEN_ID>"
            ),
        )
        return state, "resolve_and_save_feishu_manager"
    if facts["feishu_identity"] == "user" and facts["authorization_state"] == "waiting":
        state.update(
            next_question="上一次扫码授权还在等待确认：请完成页面确认；过期就重新发起。",
            next_command="manage feishu-auth start（完成后 feishu-auth complete）",
        )
        return state, "resume_existing_user_base_authorization"
    if facts["feishu_identity"] == "user" and facts["authorization_state"] != "authorized":
        state.update(
            next_question=(
                "应用已绑定但密钥/授权未就绪：Agent 会创建并打开一个本地密钥文件，"
                "把『凭证与基础信息』里的 App Secret 粘贴进去保存（不经过聊天）；"
                "随后完成一次扫码授权。"
            ),
            next_command=secret_command,
            then="manage feishu-auth start（扫码后 feishu-auth complete）",
            create_app_guide=guide,
        )
        return state, "provide_app_secret_for_private_profile"
    if state["destination"] == "undecided":
        state.update(
            next_question=(
                "文章写入飞书的哪里？① 跳过 ② 写入已有表格（把表格链接发我即可，"
                "会先只读校验字段）③ 新建标准表格（字段清单见 next_command_field_list，"
                "确认后创建；bot 身份创建并授予你管理权限，全程免扫码）"
            ),
            next_command="manage feishu-destination --mode skip|existing|create",
            next_command_existing="manage feishu-target --url <表格链接>",
            next_command_field_list=field_names,
        )
        return state, "ask_user_for_feishu_destination"
    if state["destination"] == "create" and not state["target_configured"]:
        state.update(
            next_question=(
                "将创建标准文章表，字段：" + "、".join(field_names)
                + (
                    "；bot 身份创建后会把管理权限授予你（免扫码）。确认字段与名称后继续。"
                    if feishu["identity"] == "bot"
                    else "；需要一次扫码授权（最小权限）。"
                )
            ),
            next_command=(
                "manage execution-policy set --mode autopilot --feishu-provisioning allow "
                "--base-name <名称> --table-name <表名> --feishu-sync allow --yes → "
                "manage feishu-create-base --name <名称> --table-name <表名>"
                "（策略同名精确匹配即自动授权，无需 --yes；"
                + (
                    "bot 身份会自动把管理权限授予已配置的管理员，无需再执行 grant-manager）"
                    if feishu["identity"] == "bot"
                    else "切勿自行加 --yes 绕过已固化的策略）"
                )
            ),
        )
        return state, "provision_configured_feishu_base"
    if not state["target_configured"] and state["destination"] == "existing":
        state.update(
            next_question="请提供目标表格的链接（先只读校验字段，再保存映射）。",
            next_command="manage feishu-target --url <表格链接>",
        )
        return state, "configure_existing_feishu_target"
    state.update(next_question=None, next_command="manage doctor --online（最终校验）")
    return state, "run_feishu_validation"


def next_stage(config: dict[str, Any], *, cli: dict[str, Any] | None = None) -> tuple[str, str]:
    """Compute the next safe setup action from persisted state and CLI facts."""
    facts = stage_facts(config, cli=cli)
    if not facts["redfox_key"]:
        return "redfox_credentials_missing", "run_redfox_key_setup"
    if not facts["search_window"]:
        return "search_window_unconfirmed", "ask_user_for_search_window"
    if not facts["subscriptions"]:
        return "subscriptions_missing", "ask_for_subscription_names"
    if not facts["subscriptions_resolved"]:
        return "subscriptions_unresolved", "edit_subscriptions_add_alias"
    destination = facts["feishu_destination"]
    if destination == "undecided":
        return "feishu_destination_unconfirmed", "ask_user_for_feishu_destination"
    if destination == "skip":
        if not facts["policy_confirmed"]:
            return "execution_policy_unconfirmed", "review_and_confirm_execution_policy"
        return "ready_wechat_only", "discover_articles"
    if not facts["feishu_identity_confirmed"]:
        return "feishu_identity_unconfirmed", "ask_feishu_identity_before_authorization"
    if not facts["cli_checked"]:
        return "feishu_cli_missing_or_unchecked", "ask_user_for_feishu_setup_choice"
    if not facts["cli_compatible"]:
        return "feishu_cli_incompatible", "install_compatible_lark_cli"
    if facts["bot_secret_missing"]:
        return "feishu_secret_missing", "provide_app_secret_for_private_profile"
    if facts["feishu_identity"] == "user" and facts["authorization_state"] != "authorized":
        if facts["authorization_state"] == "waiting":
            return "feishu_authorization_waiting", "resume_existing_user_base_authorization"
        return "feishu_authorization_required", "run_feishu_auth_start"
    if facts["bot_manager_missing"]:
        return "feishu_manager_missing", "resolve_and_save_feishu_manager"
    if facts["provision_incomplete"]:
        return "feishu_provision_incomplete", "rerun_feishu_create_base_to_resume"
    if not facts["feishu_target_configured"]:
        if destination == "create":
            return "feishu_target_pending", "provision_configured_feishu_base"
        return "feishu_target_missing", "configure_existing_feishu_target"
    if facts["feishu_failed"]:
        return "feishu_validation_failed", "authorize_and_run_feishu_check"
    if facts["feishu_unverified"]:
        return "feishu_unverified", "authorize_and_run_feishu_check"
    if not facts["policy_confirmed"]:
        return "execution_policy_unconfirmed", "review_and_confirm_execution_policy"
    return "ready", "discover_articles"
