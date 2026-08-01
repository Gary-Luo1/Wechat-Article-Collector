"""Single-purpose execution-policy decisions shared by setup and processing commands."""

from __future__ import annotations

from typing import Any


FEISHU_APPROVAL_SCOPE_FIELDS = (
    "destination",
    "identity",
    "binding_mode",
    "agent_source",
    "expected_app_id",
    "expected_user_open_id",
    "cli_profile",
    "manager_open_id",
    "base_token",
    "table_id",
    "schema_policy",
    "field_mapping",
)


def policy_for(config: dict[str, Any]) -> dict[str, Any]:
    """Return the persisted policy object owned by a configuration."""
    return config["setup"]["execution_policy"]


def autopilot_policy(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return a confirmed autopilot policy, or ``None`` when approval is absent."""
    policy = policy_for(config)
    if policy["confirmed"] and policy["mode"] == "autopilot":
        return policy
    return None


def invalidate_policy(config: dict[str, Any]) -> None:
    """Clear every approval that becomes unsafe after a scope-changing edit."""
    policy = policy_for(config)
    policy["confirmed"] = False
    policy["allow_feishu_provisioning"] = False
    policy["provision_base_name"] = ""
    policy["provision_table_name"] = ""
    policy["allow_feishu_sync"] = False
    policy["approved_at"] = ""


def feishu_approval_scope_changed(
    previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Whether a Feishu edit changes what the existing approval authorizes."""
    return any(previous.get(key) != current.get(key) for key in FEISHU_APPROVAL_SCOPE_FIELDS)


def invalidate_for_feishu_change(
    config: dict[str, Any], previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Invalidate once when a Feishu approval scope edit is detected."""
    if not feishu_approval_scope_changed(previous, current):
        return False
    invalidate_policy(config)
    return True


def allows_automatic_provisioning(
    config: dict[str, Any], *, base_name: str, table_name: str
) -> bool:
    """Check whether the exact requested Base creation was pre-approved."""
    policy = policy_for(config)
    return bool(
        policy["confirmed"]
        and policy["mode"] == "autopilot"
        and config["feishu"]["destination"] == "create"
        and policy["allow_feishu_provisioning"]
        and policy["provision_base_name"] == base_name
        and policy["provision_table_name"] == table_name
        and not config["feishu"]["base_token"]
        and not config["feishu"]["table_id"]
    )


def next_stage(
    config: dict[str, Any], *, cli: dict[str, Any] | None = None
) -> tuple[str, str]:
    """Compute the next safe setup action from persisted state and CLI facts."""
    if not config["wechat"]["cookie"].strip() or not config["wechat"]["token"].strip():
        return "wechat_credentials_missing", "ask_user_to_choose_chat_or_local_file"
    wechat_health = config["health"]["wechat"]
    if wechat_health["consecutive_failures"]:
        if wechat_health["last_failure_kind"] in {
            "WeChatCookieExpired",
            "WeChatTokenExpired",
            "WeChatCredentialContextError",
        }:
            return "wechat_credentials_expired", "ask_user_to_choose_chat_or_local_file"
        return "wechat_validation_failed", "run_online_doctor"
    if not wechat_health["last_verified_at"]:
        return "wechat_unverified", "run_online_doctor"
    if not config["setup"]["search_window_confirmed"]:
        return "search_window_unconfirmed", "ask_user_for_search_window"
    if not config["subscriptions"]:
        return "subscriptions_missing", "ask_for_subscription_names"
    if any(not str(item.get("biz", "")).strip() for item in config["subscriptions"]):
        return "subscriptions_unresolved", "resolve_and_confirm_subscriptions"
    destination = config["feishu"]["destination"]
    if destination == "undecided":
        return "feishu_destination_unconfirmed", "ask_user_for_feishu_destination"
    policy = policy_for(config)
    if not policy["confirmed"]:
        return "execution_policy_unconfirmed", "review_and_confirm_execution_policy"
    if destination == "skip":
        return "ready_wechat_only", "discover_articles"
    if not config["setup"]["feishu_identity_confirmed"]:
        return "feishu_identity_unconfirmed", "ask_feishu_identity_before_authorization"
    if cli is None:
        return "feishu_cli_missing_or_unchecked", "check_or_install_lark_cli"
    if not cli.get("compatible"):
        return "feishu_cli_incompatible", "install_compatible_lark_cli"
    authorization = config["setup"]["feishu_authorization"]
    if config["feishu"]["identity"] == "user" and authorization["state"] != "authorized":
        if authorization["state"] == "waiting":
            return "feishu_authorization_waiting", "resume_existing_user_base_authorization"
        return "feishu_authorization_required", "run_feishu_auth_start"
    if (
        config["feishu"]["identity"] == "bot"
        and policy["allow_feishu_provisioning"]
        and not config["feishu"]["manager_open_id"]
    ):
        return "feishu_manager_missing", "resolve_and_save_feishu_manager"
    if not (config["feishu"]["base_token"] and config["feishu"]["table_id"]):
        if destination == "create":
            return "feishu_target_pending", "provision_configured_feishu_base"
        return "feishu_target_missing", "configure_existing_feishu_target"
    feishu_health = config["health"]["feishu"]
    if feishu_health["consecutive_failures"]:
        return "feishu_validation_failed", "authorize_and_run_feishu_check"
    if not feishu_health["last_verified_at"]:
        return "feishu_unverified", "authorize_and_run_feishu_check"
    return "ready", "discover_articles"
