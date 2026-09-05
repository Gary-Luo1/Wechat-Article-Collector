"""Feishu onboarding and identity handlers for the manage command."""

from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from bitable_client import (
    create_standard_base,
    probe_app_secret_resolution,
    created_base_identifiers,
    feishu_identity_context,
    grant_bot_created_resource,
    preflight_feishu,
    resolve_lark_profile,
    standard_field_schema,
    verify_feishu_identity,
)

from config_store import (
    DEFAULT_CONFIG,
    ConfigError,
    load_config,
    modify_config,
    update_health,
)

from execution_policy import (
    allows_automatic_provisioning,
    invalidate_policy,
    policy_for,
    stage_facts,
)

from lark_runtime import (
    LarkCLIError,
    _run_lark,
    discover_global_lark_profiles,
    import_global_lark_profile,
    private_profile_secret_state,
    profile_name_for_app,
)
from protocol import (
    _pipe_cmd,
    _read_secret_stdin,
)

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


def _expected_app_id(config: dict[str, Any]) -> str:
    """Return the saved Feishu App ID, normalized."""
    return str(config["feishu"].get("expected_app_id") or "").strip()


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


def _parse_feishu_base_url(url: str) -> tuple[str, str]:
    """Extract (base_token, table_id) from a Feishu base URL."""
    import urllib.parse as _up

    raw = str(url or "").strip()
    if not raw:
        raise ValueError("provide the Feishu base URL, e.g. https://x.feishu.cn/base/BASE?table=tblX")
    parsed = _up.urlparse(raw if "://" in raw else "https://" + raw)
    parts = [part for part in parsed.path.split("/") if part]
    base_token = ""
    if "base" in parts:
        idx = parts.index("base")
        if idx + 1 < len(parts):
            base_token = parts[idx + 1]
    if not base_token:
        raise ValueError("URL does not look like a Feishu base link (expect /base/<token>)")
    query = _up.parse_qs(parsed.query)
    table_id = (query.get("table") or query.get("tableId") or [""])[0]
    if not table_id.startswith("tbl"):
        raise ValueError(
            "URL is missing the table parameter (?table=tbl...); open the exact table and copy its address bar URL"
        )
    return base_token, table_id


def _feishu_target(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Map an existing Base table by URL, verifying read access and fields."""
    from bitable_client import list_fields

    base_token, table_id = _parse_feishu_base_url(arguments.url)
    config = load_config()
    identity = config["feishu"]["identity"]
    fields = list_fields(base_token, table_id, identity=identity)

    def mutate_target(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["feishu"].update(
            {
                "destination": "existing",
                "enabled": True,
                "base_token": base_token,
                "table_id": table_id,
                "provisioning": "existing",
            }
        )
        return cfg

    saved = modify_config(mutate_target)
    return {
        "base_token": base_token,
        "table_id": table_id,
        "field_count": len(fields),
        "field_names": [str(f.get("name", "")) for f in fields],
        "enabled": saved["feishu"]["enabled"],
    }, "run_feishu_context_then_authorize_only_if_needed"


def _feishu_setup() -> tuple[dict[str, Any], str]:
    """Dialogue-ready Feishu onboarding state: what to ask, what to run next.

    A fresh user needs no app information prepared: this command reports the
    current stage, the question to put to the user, and the exact next
    command, including console guidance for creating a new app.
    """
    config = load_config()
    feishu = config["feishu"]
    app_id = _expected_app_id(config)
    secret_state = private_profile_secret_state()
    facts = stage_facts(config, profile_secret=secret_state)
    state: dict[str, Any] = {
        "identity_confirmed": facts["feishu_identity_confirmed"],
        "identity": facts["feishu_identity"],
        "app_bound": facts["app_bound"],
        "app_id": app_id,
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
        # The bot chain has no OAuth step that would surface this gap later:
        # without an App Secret every API call dead-ends, so collect it here
        # (local check only; no network, no device-auth request).
        state.update(
            next_question=(
                "bot 身份需要应用的 App Secret 才能调用飞书 API：请从开放平台应用的"
                "『凭证与基础信息』复制，用 stdin 管道提供（不经过聊天回显）。"
            ),
            next_command=_pipe_cmd(
                f"printf %s '<APP_SECRET>' | manage feishu-app-secret --app-id {app_id}"
            ),
            create_app_guide=guide,
        )
        return state, "provide_app_secret_for_private_profile"
    if facts["bot_manager_missing"]:
        known_user = ""
        try:
            known_user = _authorized_user_open_id()
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
            next_question="应用已绑定但密钥/授权未就绪：请提供 App Secret（stdin），随后完成一次扫码授权。",
            next_command=_pipe_cmd(
                f"printf %s '<APP_SECRET>' | manage feishu-app-secret --app-id {app_id}"
            ),
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
            next_command_field_list=[
                spec["name"] for spec in standard_field_schema()
            ],
        )
        return state, "ask_user_for_feishu_destination"
    if state["destination"] == "create" and not state["target_configured"]:
        state.update(
            next_question=(
                "将创建标准文章表，字段：" + "、".join(spec["name"] for spec in standard_field_schema())
                + ("；bot 身份创建后会把管理权限授予你（免扫码）。确认字段与名称后继续。"
                   if feishu["identity"] == "bot" else "；需要一次扫码授权（最小权限）。")
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
    try:
        _run_lark(
            ["config", "init", "--app-id", app_id, "--app-secret-stdin"],
            retries=1,
            input_text=secret,
        )
    except LarkCLIError as exc:
        # config init verifies the credential against Feishu's token endpoint,
        # so a failure here almost always means the secret/App-ID pair was
        # rejected; say so instead of surfacing a bare transport error.
        raise LarkCLIError(
            f"storing the App Secret failed: {exc} | the App Secret or App ID was "
            "most likely rejected — copy a fresh App Secret from the Feishu Open "
            "Platform console (凭证与基础信息) for the bound App ID and retry",
            kind=exc.kind,
            code=exc.code,
            retryable=exc.retryable,
        ) from exc
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
    try:
        grant_bot_created_resource(resource_token, arguments.resource_type, manager_open_id)
    except LarkCLIError as exc:
        if exc.kind != "duplicate":
            raise
        # Re-running the same grant reports the manager as already present;
        # treat that as success for parity with the automatic provisioning path.
    
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
            kind="confirmation_required",
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
        policy = policy_for(config)
        return {
            "preview": preview,
            "created": False,
            "policy_match": False,
            "policy_name_mismatch": bool(
                policy["confirmed"]
                and policy["mode"] == "autopilot"
                and policy["allow_feishu_provisioning"]
                and (
                    policy["provision_base_name"] != base_name
                    or policy["provision_table_name"] != table_name
                )
            ),
            # Route through the persisted policy instead of "--yes", so the
            # one-shot provisioning approval stays the only bypass-free path.
            "authorization_command": (
                "manage execution-policy set --mode autopilot --feishu-provisioning allow "
                f"--base-name {base_name} --table-name {table_name} "
                "--feishu-sync <allow|deny> [--yes]"
            ),
        }, "confirm_execution_policy_then_rerun"
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

    check = preflight_feishu(config["feishu"], allow_disabled=True)

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


def _authorized_user_open_id() -> str:
    """Read the authorized user's Open ID from the isolated lark-cli state."""
    payload = _run_lark(["auth", "status", "--json"], retries=1)
    auth = payload.get("data", payload) if isinstance(payload, dict) else {}
    identities = auth.get("identities", {}) if isinstance(auth, dict) else {}
    user = identities.get("user", {}) if isinstance(identities, dict) else {}
    return str(user.get("openId") or "").strip()


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
