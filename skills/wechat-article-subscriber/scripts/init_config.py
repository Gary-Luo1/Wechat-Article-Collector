#!/usr/bin/env python3
"""Validate and persist configuration from Agent stdin or a local wizard."""

from __future__ import annotations

import argparse
from copy import deepcopy
import getpass
import json
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any

from config_store import (
    DEFAULT_CONFIG,
    LEGACY_FIELD_MAPPING,
    ConfigError,
    load_config,
    modify_config,
    save_config,
    validate_config,
)
from execution_policy import invalidate_for_feishu_change
from paths import config_path, data_dir, secure_write_json
from protocol import dump, failure, success


MAX_AGENT_INPUT_BYTES = 256 * 1024
AGENT_INPUT_KEYS = {
    "redfox_api_key",
    "subscriptions",
    "feishu_base_token",
    "feishu_table_id",
    "feishu",
    "settings",
    "preferences",
    "execution_policy",
}

FEISHU_INPUT_KEYS = {
    "destination",
    "enabled",
    "identity",
    "binding_mode",
    "agent_source",
    "expected_app_id",
    "expected_user_open_id",
    "manager_open_id",
    "base_token",
    "table_id",
    "provisioning",
    "schema_policy",
    "field_mapping",
}
SETTINGS_INPUT_KEYS = {
    "check_hours",
    "request_delay",
    "max_articles_per_account",
    "content_dedup",
    "min_score",
    "output_language",
}
PREFERENCES_INPUT_KEYS = {
    "include_topics",
    "exclude_keywords",
    "preferred_accounts",
    "digest_hours",
    "digest_limit",
}
EXECUTION_POLICY_INPUT_KEYS = {
    "confirmed",
    "mode",
    "allow_feishu_provisioning",
    "provision_base_name",
    "provision_table_name",
    "allow_feishu_sync",
    "approved_at",
    "scope_version",
}


def _optional_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value.strip()


def _normalize_subscriptions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ConfigError("subscriptions must be a non-empty list")
    if len(value) > 100:
        raise ConfigError("subscriptions cannot contain more than 100 accounts")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            subscription = {"name": item.strip(), "alias": "", "biz": ""}
        elif isinstance(item, dict):
            unexpected = set(item) - {"name", "alias", "biz"}
            if unexpected:
                raise ConfigError(
                    f"subscriptions[{index}] contains unsupported keys: {sorted(unexpected)}"
                )
            subscription = {}
            for key in ("name", "alias", "biz"):
                raw = item.get(key, "")
                if not isinstance(raw, str):
                    raise ConfigError(f"subscriptions[{index}].{key} must be a string")
                subscription[key] = raw.strip()
        else:
            raise ConfigError(f"subscriptions[{index}] must be a name or object")
        identity = tuple(subscription[key].casefold() for key in ("name", "alias", "biz"))
        if not any(identity):
            raise ConfigError(f"subscriptions[{index}] needs name, alias, or biz")
        if identity not in seen:
            normalized.append(subscription)
            seen.add(identity)
    return normalized


def _reset_health(config: dict[str, Any], section: str) -> None:
    """Reset one health section back to its default state."""
    config["health"][section] = deepcopy(DEFAULT_CONFIG["health"][section])



def local_config_template() -> dict[str, Any]:
    """Return the smallest directly editable configuration document."""
    return {
        "version": DEFAULT_CONFIG["version"],
        "redfox": {"api_key": ""},
        "subscriptions": [],
        "feishu": {
            "destination": "undecided",
            "enabled": False,
        },
        "settings": {"check_hours": 24},
        "setup": {
            "search_window_confirmed": False,
            "execution_policy": deepcopy(DEFAULT_CONFIG["setup"]["execution_policy"]),
        },
    }


def _local_config_readiness(config: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    if not config["redfox"]["api_key"].strip():
        missing.append("redfox.api_key")
    if not config["subscriptions"]:
        missing.append("subscriptions")
    if not config["setup"]["search_window_confirmed"]:
        missing.append("settings.check_hours confirmation")
    if config["feishu"]["destination"] == "undecided":
        missing.append("feishu.destination confirmation")
    result: dict[str, Any] = {
        "path": str(config_path()),
        "valid_json": True,
        "complete": not missing,
        "missing_fields": missing,
        "credentials_echoed": False,
        "subscriptions": len(config["subscriptions"]),
        "search_window_hours": config["settings"]["check_hours"],
        "feishu_destination": config["feishu"]["destination"],
        "execution_policy_confirmed": config["setup"]["execution_policy"]["confirmed"],
    }
    return result


def _prepare_local_file(*, json_output: bool) -> int:
    target = config_path()
    created = False
    try:
        if target.exists():
            config = load_config()
        else:
            template = local_config_template()
            config = validate_config(template)
            secure_write_json(target, template)
            created = True
    except (ConfigError, OSError, UnicodeError) as exc:
        envelope = failure(
            exc,
            message=(
                "the existing local configuration is invalid and was not overwritten: "
                f"{exc}"
            ),
        )
        print(dump(envelope) if json_output else envelope["error"]["message"])
        return 1
    result = {
        **_local_config_readiness(config),
        "created": created,
        "overwritten": False,
        "encrypted": False,
        "template": local_config_template(),
    }
    next_action = "edit_local_config_file" if not result["complete"] else "run_doctor_online"
    if json_output:
        print(dump(success(result, next_action=next_action)))
    else:
        print(f"Local configuration {'created' if created else 'already exists'}: {target}")
        print("The file is plaintext JSON protected by the current OS account permissions.")
        print(json.dumps(local_config_template(), ensure_ascii=False, indent=2))
    return 0


def _validate_local_file(*, json_output: bool) -> int:
    try:
        config = load_config()
    except (ConfigError, OSError, UnicodeError) as exc:
        envelope = failure(exc, message=f"local configuration validation failed: {exc}")
        print(dump(envelope) if json_output else envelope["error"]["message"])
        return 1
    result = _local_config_readiness(config)
    next_action = "run_doctor_online" if result["complete"] else "edit_local_config_file"
    if json_output:
        print(dump(success(result, next_action=next_action)))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _launch_local_file(target: Path) -> None:
    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    command = ["open", str(target)] if sys.platform == "darwin" else ["xdg-open", str(target)]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _open_local_file(*, json_output: bool) -> int:
    target = config_path()
    if not target.is_file():
        exc = FileNotFoundError(
            f"local configuration does not exist at {target}; prepare it first"
        )
        envelope = failure(exc)
        print(dump(envelope) if json_output else envelope["error"]["message"])
        return 1
    try:
        _launch_local_file(target)
    except (OSError, subprocess.SubprocessError) as exc:
        envelope = failure(exc, message="cannot open the local configuration editor")
        print(dump(envelope) if json_output else envelope["error"]["message"])
        return 1
    result = {
        "path": str(target),
        "opened": True,
        "contents_echoed": False,
        "encrypted": False,
    }
    print(
        dump(success(result, next_action="edit_then_validate_local_config"))
        if json_output
        else f"Opened local configuration: {target}"
    )
    return 0


def setup_guide() -> dict[str, Any]:
    """Return deterministic, secret-free setup instructions for any Agent UI."""
    target = config_path()
    return {
        "input_location": {
            "choose_one": True,
            "ordinary_chat_encrypted": False,
            "not_echoing_is_encryption": False,
            "not_echoing_effect": (
                "prevents the Agent from reproducing the credential in its output, "
                "but does not remove the original chat message or prevent platform retention"
            ),
            "ordinary_chat": (
                "send the redfox API key once after acknowledging that chat may "
                "be retained; the Agent writes the configuration without echoing the value. "
                "If the key is posted in chat, treat that submission as exposure to "
                "the chat platform even when the Agent never repeats it"
            ),
            "stdin_command": (
                "printf is shell-history-safe only inside scripts; in an interactive "
                "shell prefer `cat | manage redfox-set-key` so the key never lands in "
                "command history"
            ),
            "self_edit": f"edit the local configuration file at {target}",
            "local_hidden_prompt": "run setup locally and enter values at the hidden prompts",
            "never": ["command-line arguments", "environment variables", "repository files"],
        },
        "local_config_file": {
            "path": str(target),
            "format": "UTF-8 JSON",
            "encrypted": False,
            "protection": (
                "plaintext local file protected by the current OS user account permissions; "
                "do not sync, upload, commit, or share it"
            ),
            "required_fields": {
                "redfox.api_key": (
                    "redfox.hk API key; preferred input channel is "
                    "`printf %s '<KEY>' | manage redfox-set-key`"
                ),
                "subscriptions": "account entries; each needs the WeChat alias (微信号) — the data source queries by alias only",
                "settings.check_hours": "lookback window in hours; 24 is recommended",
                "feishu.destination": (
                    "required explicit choice: skip, map an existing Base, or create a Base; "
                    "undecided is never treated as skip"
                ),
                "setup.execution_policy": (
                    "one-time bounded approval for routine work, "
                    "optional Feishu provisioning, and optional Feishu sync"
                ),
            },
            "minimal_template": local_config_template(),
            "subscription_item_example": {"name": "<EXACT_ACCOUNT_NAME>", "alias": "<WECHAT_ALIAS_REQUIRED>"},
            "prepare_command": "setup --prepare-local-file --format json",
            "open_command": "setup --open-local-file --format json",
            "validate_command": "setup --validate-local-file --format json",
        },
        "redfox_credentials": {
            "signup_url": "https://redfox.hk/",
            "steps": [
                "Register at redfox.hk and create an API key.",
                "Pipe the key into the Skill: printf %s '<KEY>' | manage redfox-set-key.",
                "Never paste the key into ordinary chat or command-line arguments.",
            ],
            "note": "paid per-call API; data covers articles from 2026-04-01 onward",
        },
        "search_window": {
            "required_question": "每次希望搜索多久以内的文章？",
            "choices": [
                {"label": "24 小时（推荐）", "hours": 24},
                {"label": "48 小时", "hours": 48},
                {"label": "7 天", "hours": 168},
                {"label": "自定义", "hours": None},
            ],
            "default_if_skipped": 24,
            "rule": "state the 24-hour default explicitly; never apply it silently",
        },
        "configuration_manifest": {
            "collect_before_execution": [
                "credential input channel",
                "redfox API key via stdin",
                "subscription account names",
                "search window",
                "whether routine Feishu provisioning and qualified-record sync are allowed",
                "whether Feishu is skipped, mapped to an existing table, or provisioned",
                "Feishu identity, exact App ID, human manager, and target or Base/table names",
                "whether routine Feishu provisioning and qualified-record sync are allowed",
            ],
            "blocking_rule": (
                "Feishu destination is a required user decision. Never infer skip from an "
                "omitted field or from deny defaults; execution remains blocked while it is "
                "undecided."
            ),
            "single_confirmation": (
                "Show one summary of these choices, then persist it with "
                "manage execution-policy set ... --yes. Do not ask again for an "
                "operation already covered by the unchanged policy."
            ),
            "agent_continues_automatically": [
                "validate credentials",
                "resolve exact subscriptions",
                "reuse or start the one required authorization flow",
                "provision and verify the configured standard Base",
                "discover, read, score, queue, export, and sync qualified articles",
            ],
            "unavoidable_pause": [
                "the user must complete a Feishu OAuth/device page",
                "an account match remains ambiguous",
                "credentials expire or a new platform scope is required",
                "the App, identity, manager, target, schema, or approved scope changes",
                "a delete, reset, or other destructive action is requested",
            ],
        },
        "execution_policy": {
            "default": deepcopy(DEFAULT_CONFIG["setup"]["execution_policy"]),
            "show_command": "manage execution-policy show",
            "set_command": (
                "manage execution-policy set --mode autopilot "
                "--feishu-provisioning allow|deny "
                "--feishu-sync allow|deny "
                "[--base-name <BASE> --table-name <TABLE>] --yes"
            ),
            "boundary": (
                "Autopilot never authorizes deletion, reset, profile mutation, a new "
                "App/identity/manager/target, schema expansion, new OAuth scopes, or "
                "a forced below-threshold Feishu write."
            ),
        },
    }


def _normalize_feishu(
    value: Any, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    if value is None:
        return deepcopy(DEFAULT_CONFIG["feishu"])
    if not isinstance(value, dict):
        raise ConfigError("feishu must be an object")
    unexpected = set(value) - FEISHU_INPUT_KEYS
    if unexpected:
        raise ConfigError(f"feishu contains unsupported keys: {sorted(unexpected)}")
    normalized = deepcopy(DEFAULT_CONFIG["feishu"])
    normalized.update(value)
    if existing is not None:
        # Partial patches keep every omitted field unchanged. Comparing against
        # rebuilt defaults would treat untouched binding/identity/mapping fields
        # as "scope changed" and spuriously invalidate the execution policy.
        for key in set(normalized) - set(value):
            normalized[key] = deepcopy(existing.get(key, DEFAULT_CONFIG["feishu"][key]))
        if (
            str(existing.get("expected_app_id") or "").strip()
            != str(normalized.get("expected_app_id") or "").strip()
        ):
            # cli_profile is derived from the App ID; a new App ID must not
            # inherit the old profile.
            normalized["cli_profile"] = ""
    if "destination" not in value:
        if any(key in value for key in ("base_token", "table_id", "provisioning", "enabled")):
            has_target = bool(normalized.get("base_token")) and bool(normalized.get("table_id"))
            if normalized.get("provisioning") == "created":
                normalized["destination"] = "create"
            elif has_target or normalized.get("provisioning") == "existing":
                normalized["destination"] = "existing"
            elif value.get("enabled") is False:
                # Backward-compatible explicit skip. Omitting the entire Feishu
                # object still leaves the full setup in the undecided state.
                normalized["destination"] = "skip"
    # Supplying a complete target means sync is intentionally enabled unless
    # the Agent explicitly sends enabled=false. Only a target supplied by this
    # patch (not one preserved from the existing config) implies enablement.
    if (
        "enabled" not in value
        and "base_token" in value
        and "table_id" in value
        and normalized.get("base_token")
        and normalized.get("table_id")
    ):
        normalized["enabled"] = True
    return normalized


def _record_feishu_identity_choice(config: dict[str, Any], value: Any) -> None:
    if not isinstance(value, dict) or "identity" not in value:
        return
    identity = str(value["identity"])
    authorization = config["setup"]["feishu_authorization"]
    if (
        not config["setup"]["feishu_identity_confirmed"]
        or authorization.get("identity") != identity
    ):
        config["setup"]["feishu_authorization"] = {
            **dict(DEFAULT_CONFIG["setup"]["feishu_authorization"]),
            "state": "not_required" if identity == "bot" else "not_started",
            "identity": identity,
        }
    config["setup"]["feishu_identity_confirmed"] = True


def config_from_agent_payload(
    payload: Any, *, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConfigError("Agent configuration must be a JSON object")
    unexpected = set(payload) - AGENT_INPUT_KEYS
    if unexpected:
        raise ConfigError(f"Agent configuration contains unsupported keys: {sorted(unexpected)}")
    if "feishu" in payload and (
        "feishu_base_token" in payload or "feishu_table_id" in payload
    ):
        raise ConfigError("use feishu or legacy Feishu fields, not both")
    previous_feishu: dict[str, Any] | None = None
    if existing is not None:
        existing = validate_config(deepcopy(existing))
        previous_feishu = deepcopy(existing["feishu"])
    if "feishu" in payload:
        feishu = _normalize_feishu(
            payload["feishu"],
            existing=existing["feishu"] if existing is not None else None,
        )
    else:
        base_token = _optional_string(payload, "feishu_base_token")
        table_id = _optional_string(payload, "feishu_table_id")
        if bool(base_token) != bool(table_id):
            raise ConfigError("provide both Feishu Base token and table ID, or leave both empty")
        feishu = (
            deepcopy(existing["feishu"])
            if existing is not None
            else deepcopy(DEFAULT_CONFIG["feishu"])
        )
        if base_token and table_id:
            feishu.update(
                {
                    "destination": "existing",
                    "enabled": True,
                    "base_token": base_token,
                    "table_id": table_id,
                    "provisioning": "existing",
                    "field_mapping": deepcopy(LEGACY_FIELD_MAPPING),
                }
            )
        elif "feishu_base_token" in payload or "feishu_table_id" in payload:
            feishu["destination"] = "skip"
    config = deepcopy(existing) if existing is not None else deepcopy(DEFAULT_CONFIG)
    if "redfox_api_key" in payload:
        api_key = str(payload.get("redfox_api_key") or "").strip()
        if not api_key:
            raise ConfigError("redfox_api_key must be a non-empty string")
        config["redfox"] = {"api_key": api_key}
    elif existing is None and not config["redfox"]["api_key"].strip():
        raise ConfigError("first-time setup requires redfox_api_key")
    if "subscriptions" in payload:
        config["subscriptions"] = _normalize_subscriptions(
            payload.get("subscriptions")
        )
        _reset_health(config, "subscriptions")
    elif existing is None:
        # First-time setup still requires a non-empty subscription list.
        config["subscriptions"] = _normalize_subscriptions(
            payload.get("subscriptions")
        )
    config["feishu"] = feishu
    if "feishu" in payload:
        _record_feishu_identity_choice(config, payload["feishu"])
        if previous_feishu is not None:
            invalidate_for_feishu_change(config, previous_feishu, config["feishu"])
    if "settings" in payload:
        config["settings"] = _normalize_settings(
            payload["settings"], partial=True, existing=config["settings"]
        )
        if "check_hours" in payload["settings"]:
            config["setup"]["search_window_confirmed"] = True
    if "preferences" in payload:
        config["preferences"] = _normalize_preferences(
            payload["preferences"],
            partial=True,
            existing=config["preferences"],
        )
    if "execution_policy" in payload:
        config["setup"]["execution_policy"] = _normalize_execution_policy(
            payload["execution_policy"],
            partial=True,
            existing=config["setup"]["execution_policy"],
        )
    return validate_config(config)


def _normalize_settings(
    value: Any, *, partial: bool, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("settings must be an object")
    unexpected = set(value) - SETTINGS_INPUT_KEYS
    if unexpected:
        raise ConfigError(f"settings contains unsupported keys: {sorted(unexpected)}")
    normalized = (
        deepcopy(existing)
        if partial and existing is not None
        else deepcopy(DEFAULT_CONFIG["settings"])
    )
    if partial:
        normalized.update(value)
    else:
        normalized = dict(value)
    return normalized


def _normalize_preferences(
    value: Any, *, partial: bool, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("preferences must be an object")
    unexpected = set(value) - PREFERENCES_INPUT_KEYS
    if unexpected:
        raise ConfigError(f"preferences contains unsupported keys: {sorted(unexpected)}")
    normalized = (
        deepcopy(existing)
        if partial and existing is not None
        else (deepcopy(DEFAULT_CONFIG["preferences"]) if partial else {})
    )
    normalized.update(value)
    return normalized


def _normalize_execution_policy(
    value: Any, *, partial: bool, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("execution_policy must be an object")
    unexpected = set(value) - EXECUTION_POLICY_INPUT_KEYS
    if unexpected:
        raise ConfigError(
            f"execution_policy contains unsupported keys: {sorted(unexpected)}"
        )
    if partial and existing is not None:
        # Partial patches keep every omitted policy field unchanged; rebuilding
        # from defaults would reset confirmed/sync flags on every patch.
        normalized = deepcopy(existing)
    else:
        normalized = (
            deepcopy(DEFAULT_CONFIG["setup"]["execution_policy"]) if partial else {}
        )
    normalized.update(value)
    return normalized


def _apply_section_patch(
    config: dict[str, Any], section: str, payload: Any
) -> dict[str, Any]:
    if section == "feishu":
        previous_feishu = config["feishu"]
        normalized_feishu = _normalize_feishu(payload, existing=config["feishu"])
        config["feishu"] = normalized_feishu
        invalidate_for_feishu_change(config, previous_feishu, normalized_feishu)
        _record_feishu_identity_choice(config, payload)
    elif section == "subscriptions":
        value = payload.get("subscriptions") if isinstance(payload, dict) else payload
        config["subscriptions"] = _normalize_subscriptions(value)
        _reset_health(config, "subscriptions")
    elif section == "settings":
        if not isinstance(payload, dict):
            raise ConfigError("settings must be an object")
        unexpected = set(payload) - SETTINGS_INPUT_KEYS
        if unexpected:
            raise ConfigError(f"settings contains unsupported keys: {sorted(unexpected)}")
        updates = dict(payload)
        config["settings"].update(updates)
        if "check_hours" in updates:
            config["setup"]["search_window_confirmed"] = True
    elif section == "preferences":
        config["preferences"] = _normalize_preferences(payload, partial=True)
    elif section == "execution_policy":
        config["setup"]["execution_policy"] = _normalize_execution_policy(
            payload, partial=True, existing=config["setup"]["execution_policy"]
        )
    elif section == "redfox":
        if not isinstance(payload, dict):
            raise ConfigError("redfox credential update must be an object")
        unexpected = set(payload) - {"api_key"}
        if unexpected:
            raise ConfigError(f"redfox update contains unsupported keys: {sorted(unexpected)}")
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            raise ConfigError("redfox.api_key must be a non-empty string")
        config["redfox"] = {"api_key": api_key}
    else:
        raise ConfigError(f"unsupported setup section: {section}")
    return validate_config(config)


def _save_agent_raw(raw: str, *, section: str = "full", json_output: bool = False) -> int:
    if len(raw.encode("utf-8")) > MAX_AGENT_INPUT_BYTES:
        logging.error("Agent configuration exceeds the input size limit")
        return 1
    try:
        payload = json.loads(raw.lstrip("\ufeff"))
        if section == "full":
            try:
                config = modify_config(
                    lambda current: config_from_agent_payload(payload, existing=current)
                )
            except ConfigError as exc:
                if "configuration not found" not in str(exc):
                    raise
                # First-time setup: no existing config to merge, build from defaults.
                config = config_from_agent_payload(payload)
                save_config(config)
            path = config_path()
        else:
            config = modify_config(
                lambda current: _apply_section_patch(current, section, payload)
            )
            path = config_path()
    except (ConfigError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        if json_output:
            print(dump(failure(exc)))
        else:
            logging.error("Cannot save Agent configuration: %s", exc)
        return 1
    result = {
        "path": str(path),
        "section": section,
        "subscriptions": len(config["subscriptions"]),
        "feishu_enabled": config["feishu"]["enabled"],
        "feishu_destination": config["feishu"]["destination"],
        "search_window_hours": config["settings"]["check_hours"],
        "search_window_confirmed": config["setup"]["search_window_confirmed"],
        "execution_policy_confirmed": config["setup"]["execution_policy"]["confirmed"],
        "credentials_echoed": False,
    }
    if not config["setup"]["search_window_confirmed"]:
        next_action = "ask_user_for_search_window"
    elif config["feishu"]["destination"] == "undecided":
        next_action = "ask_user_for_feishu_destination"
    else:
        next_action = "run_doctor_online"
    if json_output:
        print(dump(success(result, next_action=next_action)))
        return 0
    print(f"Configuration saved with restricted permissions: {path}")
    print(
        f"Configured {len(config['subscriptions'])} subscription(s); "
        f"Feishu sync {'enabled' if config['feishu']['enabled'] else 'disabled'}."
    )
    print("Credential values were not echoed.")
    if not config["setup"]["search_window_confirmed"]:
        print(
            "Search window is not confirmed. Ask for 24 hours (recommended), "
            "48 hours, 7 days, or a custom value before continuing."
        )
    return 0


def _agent_stdin_setup(*, section: str = "full", json_output: bool = False) -> int:
    if sys.stdin.isatty():
        exc = ValueError("--agent-stdin requires a JSON document on standard input")
        print(dump(failure(exc))) if json_output else logging.error("%s", exc)
        return 1
    return _save_agent_raw(
        sys.stdin.read(MAX_AGENT_INPUT_BYTES + 1),
        section=section,
        json_output=json_output,
    )


def _prepare_agent_file(*, json_output: bool = False) -> int:
    root = data_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".agent-config-",
            suffix=".json",
            dir=root,
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        finally:
            os.close(descriptor)
    except OSError as exc:
        print(dump(failure(exc, message="cannot prepare Agent configuration inbox"))) if json_output else logging.error("Cannot prepare Agent configuration inbox: %s", exc)
        return 1
    path = str(Path(name).resolve())
    print(dump(success({"inbox_path": path})) if json_output else path)
    return 0


def _scoped_agent_file(value: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ConfigError("Agent configuration inbox cannot be a symbolic link")
    resolved = candidate.resolve()
    root = data_dir().resolve()
    if resolved.parent != root:
        raise ConfigError("Agent configuration inbox must be inside the application state directory")
    if not resolved.name.startswith(".agent-config-") or resolved.suffix != ".json":
        raise ConfigError("Agent configuration inbox has an invalid name")
    return resolved


def _agent_file_setup(
    value: Path, *, section: str = "full", json_output: bool = False
) -> int:
    try:
        inbox = _scoped_agent_file(value)
    except (ConfigError, OSError) as exc:
        print(dump(failure(exc))) if json_output else logging.error("Cannot use Agent configuration inbox: %s", exc)
        return 1
    try:
        if os.name != "nt":
            inbox.chmod(stat.S_IRUSR | stat.S_IWUSR)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(inbox, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            raw = handle.read(MAX_AGENT_INPUT_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        try:
            inbox.unlink(missing_ok=True)
        except OSError:
            pass
        print(dump(failure(exc, message="cannot read Agent configuration inbox"))) if json_output else logging.error("Cannot read Agent configuration inbox: %s", exc)
        return 1
    try:
        inbox.unlink(missing_ok=True)
    except OSError as exc:
        print(dump(failure(exc, message="cannot remove consumed Agent configuration inbox"))) if json_output else logging.error("Cannot remove consumed Agent configuration inbox: %s", exc)
        return 1
    return _save_agent_raw(raw, section=section, json_output=json_output)


def _prompt_number(label: str, default: float, minimum: float, maximum: float) -> float:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        try:
            value = float(raw) if raw else float(default)
        except ValueError:
            print("Enter a number")
            continue
        if minimum <= value <= maximum:
            return value
        print(f"Enter a value between {minimum} and {maximum}")


def _interactive_setup() -> int:
    print("WeChat Article Subscriber — local setup (redfox data source)")
    print("Credentials are entered locally and are not sent to an AI conversation.")
    print("Create an API key at https://redfox.hk/ first.")
    api_key = getpass.getpass("redfox API key (hidden): ").strip()
    if not api_key:
        print("The redfox API key is required")
        return 1
    subscriptions = []
    print("Add exact account names and/or WeChat aliases. Blank name finishes the list.")
    while True:
        name = input("Account name: ").strip()
        if not name:
            if subscriptions:
                break
            print("Add at least one account")
            continue
        alias = input("WeChat alias (recommended, optional): ").strip()
        subscriptions.append({"name": name, "alias": alias})
    print(
        "Choose the Feishu destination now; an omitted choice is never treated as skip."
    )
    while True:
        feishu_choice = input(
            "Feishu destination [skip/existing/create]: "
        ).strip().casefold()
        if feishu_choice in {"skip", "existing", "create"}:
            break
        print("Enter skip, existing, or create.")
    check_hours = _prompt_number("Lookback hours", 24, 1, 8760)
    request_delay = _prompt_number("Request delay seconds", 3, 0, 60)
    min_score = _prompt_number("Minimum Feishu score", 6, 1, 10)
    config = {
        **deepcopy(DEFAULT_CONFIG),
        "redfox": {"api_key": api_key},
        "subscriptions": subscriptions,
        "feishu": {
            **deepcopy(DEFAULT_CONFIG["feishu"]),
            "destination": feishu_choice,
        },
        "settings": {
            **DEFAULT_CONFIG["settings"],
            "check_hours": check_hours,
            "request_delay": request_delay,
            "min_score": min_score,
        },
        "setup": {
            "search_window_confirmed": True,
            "feishu_identity_confirmed": False,
        },
    }
    try:
        path = save_config(config)
    except (ConfigError, OSError) as exc:
        logging.error("%s", exc)
        return 1
    print(f"Configuration saved with restricted permissions: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--section",
        choices=(
            "full",
            "subscriptions",
            "settings",
            "preferences",
            "feishu",
            "execution_policy",
            "redfox",
        ),
        default="full",
        help="configuration section for --agent-stdin/--agent-file",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument(
        "--guide",
        action="store_true",
        help="print secret-free dialogue setup guidance without changing configuration",
    )
    sources.add_argument(
        "--prepare-local-file",
        action="store_true",
        help="create a non-secret editable local config skeleton without overwriting",
    )
    sources.add_argument(
        "--validate-local-file",
        action="store_true",
        help="validate the local config and print only redacted readiness",
    )
    sources.add_argument(
        "--open-local-file",
        action="store_true",
        help="open the existing local config in the OS default editor",
    )
    sources.add_argument(
        "--agent-stdin",
        action="store_true",
        help="read a bounded Agent configuration JSON object from standard input",
    )
    sources.add_argument(
        "--feishu-agent-stdin",
        action="store_true",
        help="merge a bounded Feishu configuration JSON object from standard input",
    )
    sources.add_argument(
        "--prepare-agent-file",
        action="store_true",
        help="create a restricted one-time inbox for Agents without standard input",
    )
    sources.add_argument(
        "--agent-file",
        type=Path,
        help="consume and delete a prepared one-time Agent configuration inbox",
    )
    sources.add_argument(
        "--feishu-agent-file",
        type=Path,
        help="merge and delete a prepared one-time Feishu configuration inbox",
    )
    arguments = parser.parse_args(argv)
    json_output = arguments.format == "json"
    if arguments.guide:
        guide = setup_guide()
        if json_output:
            print(dump(success(guide, next_action="ask_user_to_choose_chat_or_local_file")))
        else:
            print(json.dumps(guide, ensure_ascii=False, indent=2))
        return 0
    if arguments.prepare_local_file:
        return _prepare_local_file(json_output=json_output)
    if arguments.validate_local_file:
        return _validate_local_file(json_output=json_output)
    if arguments.open_local_file:
        return _open_local_file(json_output=json_output)
    if arguments.agent_stdin:
        return _agent_stdin_setup(section=arguments.section, json_output=json_output)
    if arguments.feishu_agent_stdin:
        return _agent_stdin_setup(section="feishu", json_output=json_output)
    if arguments.prepare_agent_file:
        return _prepare_agent_file(json_output=json_output)
    if arguments.agent_file:
        return _agent_file_setup(
            arguments.agent_file, section=arguments.section, json_output=json_output
        )
    if arguments.feishu_agent_file:
        return _agent_file_setup(
            arguments.feishu_agent_file, section="feishu", json_output=json_output
        )
    return _interactive_setup()


if __name__ == "__main__":
    raise SystemExit(main())
