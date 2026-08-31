"""Stable machine-readable envelopes shared by Skill commands."""

from __future__ import annotations

import json
from typing import Any


NEXT_ACTIONS = {
    "CONFIG_ERROR": "prepare_or_validate_local_config",
    "REDFOX_AUTH": "run_redfox_key_setup",
    "REDFOX_RATE_LIMITED": "wait_before_retry",
    "REDFOX_TRANSIENT": "retry_with_backoff",
    "REDFOX_API_ERROR": "inspect_redfox_diagnostics",
    "REDFOX_ACCOUNT_AMBIGUOUS": "ask_user_to_disambiguate",
    "ARTICLE_READ_REQUIRED": "read_article_before_completion",
    "ARTICLE_NOT_SYNCABLE": "restore_and_complete_the_article_first",
    "LARK_API": "inspect_command_help",
    "LARK_COMMAND": "inspect_command_help",
    "ARTICLE_NOT_FOUND": "show_article_inbox",
    "INVALID_ARGUMENT": "inspect_command_help",
    "LARK_MISSING_CLI": "install_compatible_lark_cli",
    "LARK_VERSION": "install_compatible_lark_cli",
    "LARK_AUTHORIZATION": "run_feishu_auth_start",
    "LARK_PERMISSION": "fix_base_share_or_role_permission",
    "LARK_FIELD_MAPPING": "inspect_and_confirm_field_mapping",
    "LARK_WRONG_APP": "select_expected_lark_profile",
    "LARK_CONFIRMATION_REQUIRED": "ask_user_for_explicit_confirmation",
    "LARK_DUPLICATE": "treat_as_already_done",
    "LARK_CONFIG": "inspect_command_help",
    "LARK_TRANSIENT": "retry_with_backoff",
    "INTERNAL_ERROR": "report_redacted_diagnostics",
}


def success(data: Any = None, *, next_action: str = "none", meta: dict | None = None) -> dict:
    envelope: dict[str, Any] = {"ok": True, "data": data, "next_action": next_action}
    if meta:
        envelope["meta"] = meta
    return envelope


def classify_exception(exc: Exception) -> tuple[str, bool]:
    code = getattr(exc, "code", "")
    if isinstance(code, str) and (
        code.startswith("ARTICLE_") or code.startswith("REDFOX_")
    ):
        return code, bool(getattr(exc, "retryable", False))
    name = type(exc).__name__
    if name == "ConfigError":
        return "CONFIG_ERROR", False
    if name == "LarkCLIError":
        kind = str(getattr(exc, "kind", "api")).upper()
        aliases = {
            "MISSING_CLI": "LARK_MISSING_CLI",
            "VERSION": "LARK_VERSION",
            "AUTHORIZATION": "LARK_AUTHORIZATION",
            "PERMISSION": "LARK_PERMISSION",
            "FIELD_MAPPING": "LARK_FIELD_MAPPING",
            "WRONG_APP": "LARK_WRONG_APP",
            "CONFIRMATION_REQUIRED": "LARK_CONFIRMATION_REQUIRED",
            "TRANSIENT": "LARK_TRANSIENT",
        }
        return aliases.get(kind, f"LARK_{kind}"), bool(getattr(exc, "retryable", False))
    if isinstance(exc, LookupError):
        return "ARTICLE_NOT_FOUND", False
    if isinstance(exc, (ValueError, TypeError)):
        return "INVALID_ARGUMENT", False
    return "INTERNAL_ERROR", False


def failure(exc: Exception, *, message: str | None = None) -> dict:
    code, retryable = classify_exception(exc)
    safe_message = (message if message is not None else str(exc))[:500]
    envelope = {
        "ok": False,
        "error": {
            "code": code,
            "message": safe_message,
            "retryable": retryable,
            "next_action": NEXT_ACTIONS.get(code, "inspect_command_help"),
        },
    }
    if code.startswith(("ARTICLE_", "REDFOX_")):
        details = getattr(exc, "details", None)
        if isinstance(details, dict):
            envelope["error"]["details"] = details
    return envelope


def dump(envelope: dict[str, Any]) -> str:
    return json.dumps(envelope, ensure_ascii=False)
