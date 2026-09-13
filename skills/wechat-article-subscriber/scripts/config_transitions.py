"""Domain configuration transitions with one atomic persistence boundary.

Command adapters provide transport and presentation.  This module accepts the
configuration intent they already expose, normalizes it against the current
schema, applies cross-field rules and derived-state invalidation, then writes
the validated result through ``config_store.modify_config``.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_store import (
    DEFAULT_CONFIG,
    LEGACY_FIELD_MAPPING,
    ConfigError,
    modify_config,
    save_config,
    validate_config,
)

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


def invalidate_policy(config: dict[str, Any]) -> None:
    policy = config["setup"]["execution_policy"]
    policy["confirmed"] = False
    policy["allow_feishu_provisioning"] = False
    policy["provision_base_name"] = ""
    policy["provision_table_name"] = ""
    policy["allow_feishu_sync"] = False
    policy["approved_at"] = ""


def feishu_approval_scope_changed(
    previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    return any(previous.get(key) != current.get(key) for key in FEISHU_APPROVAL_SCOPE_FIELDS)


def invalidate_for_feishu_change(
    config: dict[str, Any], previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    if not feishu_approval_scope_changed(previous, current):
        return False
    invalidate_policy(config)
    return True

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


def normalize_subscriptions(value: Any) -> list[dict[str, str]]:
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
    config["health"][section] = deepcopy(DEFAULT_CONFIG["health"][section])


def normalize_feishu(
    value: Any, *, existing: dict[str, Any] | None = None
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
        for key in set(normalized) - set(value):
            normalized[key] = deepcopy(existing.get(key, DEFAULT_CONFIG["feishu"][key]))
        if (
            str(existing.get("expected_app_id") or "").strip()
            != str(normalized.get("expected_app_id") or "").strip()
        ):
            normalized["cli_profile"] = ""
    if "destination" not in value and any(
        key in value for key in ("base_token", "table_id", "provisioning", "enabled")
    ):
        has_target = bool(normalized.get("base_token")) and bool(normalized.get("table_id"))
        if normalized.get("provisioning") == "created":
            normalized["destination"] = "create"
        elif has_target or normalized.get("provisioning") == "existing":
            normalized["destination"] = "existing"
        elif value.get("enabled") is False:
            normalized["destination"] = "skip"
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


def _merge_section(
    value: Any,
    *,
    label: str,
    keys: set[str],
    defaults: dict[str, Any],
    partial: bool,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object")
    unexpected = set(value) - keys
    if unexpected:
        raise ConfigError(f"{label} contains unsupported keys: {sorted(unexpected)}")
    if partial and existing is not None:
        normalized = deepcopy(existing)
    else:
        normalized = deepcopy(defaults) if partial else {}
    normalized.update(value)
    return normalized


def normalize_settings(
    value: Any, *, partial: bool, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _merge_section(
        value,
        label="settings",
        keys=SETTINGS_INPUT_KEYS,
        defaults=DEFAULT_CONFIG["settings"],
        partial=partial,
        existing=existing,
    )


def normalize_preferences(
    value: Any, *, partial: bool, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _merge_section(
        value,
        label="preferences",
        keys=PREFERENCES_INPUT_KEYS,
        defaults=DEFAULT_CONFIG["preferences"],
        partial=partial,
        existing=existing,
    )


def normalize_execution_policy(
    value: Any, *, partial: bool, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _merge_section(
        value,
        label="execution_policy",
        keys=EXECUTION_POLICY_INPUT_KEYS,
        defaults=DEFAULT_CONFIG["setup"]["execution_policy"],
        partial=partial,
        existing=existing,
    )


def apply_agent_payload(
    payload: Any, *, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Apply a full Agent payload to a validated configuration snapshot."""
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
        feishu = normalize_feishu(
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
        config["subscriptions"] = normalize_subscriptions(payload.get("subscriptions"))
        _reset_health(config, "subscriptions")
    elif existing is None:
        config["subscriptions"] = normalize_subscriptions(payload.get("subscriptions"))
    config["feishu"] = feishu
    if "feishu" in payload:
        _record_feishu_identity_choice(config, payload["feishu"])
        if previous_feishu is not None:
            invalidate_for_feishu_change(config, previous_feishu, config["feishu"])
    if "settings" in payload:
        config["settings"] = normalize_settings(
            payload["settings"], partial=True, existing=config["settings"]
        )
        if "check_hours" in payload["settings"]:
            config["setup"]["search_window_confirmed"] = True
    if "preferences" in payload:
        config["preferences"] = normalize_preferences(
            payload["preferences"], partial=True, existing=config["preferences"]
        )
    if "execution_policy" in payload:
        config["setup"]["execution_policy"] = normalize_execution_policy(
            payload["execution_policy"],
            partial=True,
            existing=config["setup"]["execution_policy"],
        )
    return validate_config(config)


def apply_section_patch(
    config: dict[str, Any], section: str, payload: Any
) -> dict[str, Any]:
    """Apply one named domain patch, including derived state updates."""
    if section == "feishu":
        previous_feishu = deepcopy(config["feishu"])
        normalized_feishu = normalize_feishu(payload, existing=config["feishu"])
        config["feishu"] = normalized_feishu
        invalidate_for_feishu_change(config, previous_feishu, normalized_feishu)
        _record_feishu_identity_choice(config, payload)
    elif section == "subscriptions":
        value = payload.get("subscriptions") if isinstance(payload, dict) else payload
        config["subscriptions"] = normalize_subscriptions(value)
        _reset_health(config, "subscriptions")
    elif section == "settings":
        config["settings"] = normalize_settings(
            payload, partial=True, existing=config["settings"]
        )
        if "check_hours" in payload:
            config["setup"]["search_window_confirmed"] = True
    elif section == "preferences":
        config["preferences"] = normalize_preferences(payload, partial=True)
    elif section == "execution_policy":
        config["setup"]["execution_policy"] = normalize_execution_policy(
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


def _authorization(config: dict[str, Any]) -> dict[str, Any]:
    return config["setup"]["feishu_authorization"]


def _reset_authorization(config: dict[str, Any], identity: str) -> None:
    state = "not_required" if identity == "bot" else "not_started"
    _authorization(config).clear()
    _authorization(config).update(
        {
            **dict(DEFAULT_CONFIG["setup"]["feishu_authorization"]),
            "state": state,
            "identity": identity,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _feishu_scope(config: dict[str, Any]) -> tuple[str, ...]:
    feishu = config["feishu"]
    return (
        str(feishu.get("identity") or ""),
        str(feishu.get("binding_mode") or ""),
        str(feishu.get("agent_source") or ""),
        str(feishu.get("expected_app_id") or ""),
        str(feishu.get("manager_open_id") or ""),
    )


def _require_app_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("Feishu App ID must be a string")
    normalized = value.strip()
    if not re.fullmatch(r"cli_[A-Za-z0-9]+", normalized):
        raise ConfigError("Feishu App ID must start with cli_ and contain only letters/digits")
    return normalized


def _apply_domain_transition(
    config: dict[str, Any], intent: str, value: Any, state: dict[str, Any]
) -> dict[str, Any]:
    """Mutate one setup domain intent while holding the config transaction lock."""
    if intent == "feishu_destination":
        if value not in {"skip", "existing", "create"}:
            raise ConfigError("destination must be skip, existing, or create")
        previous = str(config["feishu"].get("destination") or "undecided")
        config["feishu"]["destination"] = value
        if value == "skip":
            config["feishu"]["enabled"] = False
        changed = previous != value
        if changed:
            invalidate_policy(config)
        state.update(previous=previous, changed=changed)
        return config

    if intent == "feishu_agent_context":
        if not isinstance(value, dict):
            raise ConfigError("Feishu Agent context must be an object")
        source = str(value.get("source") or "").strip()
        app_id = _require_app_id(value.get("app_id"))
        sender_open_id = str(value.get("sender_open_id") or "").strip()
        if not source:
            raise ConfigError("Feishu Agent source is required")
        if not sender_open_id.startswith("ou_"):
            raise ConfigError("trusted Feishu host sender Open ID must start with ou_")
        destination = config["feishu"]["destination"]
        if destination not in {"existing", "create"}:
            raise ConfigError(
                "choose existing or create as the Feishu destination before importing "
                "the current bot context"
            )
        if (
            config["setup"]["feishu_identity_confirmed"]
            and config["feishu"]["identity"] != "bot"
        ):
            raise ConfigError(
                "the current setup already confirms user identity; do not silently switch "
                "it to the conversational bot"
            )
        expected_app_id = str(config["feishu"].get("expected_app_id") or "").strip()
        if expected_app_id and expected_app_id != app_id:
            raise ConfigError(
                "the current Feishu conversation App ID conflicts with the saved App ID"
            )
        manager_open_id = str(config["feishu"].get("manager_open_id") or "").strip()
        if manager_open_id and manager_open_id != sender_open_id:
            raise ConfigError(
                "the current Feishu sender conflicts with the saved human manager"
            )
        before = _feishu_scope(config)
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
        changed = before != _feishu_scope(config)
        if changed:
            config["health"]["feishu"] = deepcopy(DEFAULT_CONFIG["health"]["feishu"])
            invalidate_policy(config)
        state["changed"] = changed
        return config

    if intent == "feishu_identity":
        if value not in {"user", "bot"}:
            raise ConfigError("identity must be user or bot")
        previous = str(config["feishu"].get("identity") or "user")
        was_confirmed = bool(config["setup"]["feishu_identity_confirmed"])
        config["feishu"]["identity"] = value
        config["setup"]["feishu_identity_confirmed"] = True
        changed = previous != value or not was_confirmed
        if changed:
            config["health"]["feishu"] = deepcopy(DEFAULT_CONFIG["health"]["feishu"])
            _reset_authorization(config, value)
            invalidate_policy(config)
        state.update(previous=previous, changed=changed)
        return config

    if intent == "feishu_app":
        if not isinstance(value, dict):
            raise ConfigError("Feishu App selection must be an object")
        app_id = _require_app_id(value.get("app_id"))
        profile = str(value.get("profile") or "").strip()
        if not profile:
            raise ConfigError("Feishu lark-cli profile is required")
        if not config["setup"]["feishu_identity_confirmed"]:
            raise ConfigError("select user or bot identity before selecting the Feishu app")
        previous = str(config["feishu"].get("expected_app_id") or "")
        config["feishu"]["expected_app_id"] = app_id
        config["feishu"]["cli_profile"] = profile
        if not config["feishu"].get("binding_mode"):
            config["feishu"]["binding_mode"] = "existing"
        changed = previous != app_id
        if changed:
            config["health"]["feishu"] = deepcopy(DEFAULT_CONFIG["health"]["feishu"])
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
        state["changed"] = changed
        return config

    if intent == "feishu_manager":
        normalized = str(value or "").strip()
        if not normalized.startswith("ou_"):
            raise ConfigError("manager Open ID must start with ou_")
        if (
            not config["setup"]["feishu_identity_confirmed"]
            or config["feishu"]["identity"] != "bot"
        ):
            raise ConfigError("select and confirm bot identity before setting its human manager")
        previous = str(config["feishu"].get("manager_open_id") or "")
        config["feishu"]["manager_open_id"] = normalized
        changed = previous != normalized
        if changed:
            invalidate_policy(config)
        state.update(previous=previous, changed=changed)
        return config

    if intent == "feishu_profile":
        profile = str(value or "").strip()
        if not profile:
            raise ConfigError("Feishu lark-cli profile is required")
        config["feishu"]["cli_profile"] = profile
        return config

    if intent == "feishu_target":
        if not isinstance(value, dict):
            raise ConfigError("Feishu target must be an object")
        base_token = str(value.get("base_token") or "").strip()
        table_id = str(value.get("table_id") or "").strip()
        if not base_token or not table_id:
            raise ConfigError("Feishu target requires both a Base token and table ID")
        previous = deepcopy(config["feishu"])
        config["feishu"].update(
            {
                "destination": "existing",
                "enabled": True,
                "base_token": base_token,
                "table_id": table_id,
                "provisioning": "existing",
            }
        )
        changed = invalidate_for_feishu_change(config, previous, config["feishu"])
        state["changed"] = changed
        return config

    if intent == "feishu_provision_anchor":
        if not isinstance(value, dict):
            raise ConfigError("Feishu provisioning anchor must be an object")
        base_token = str(value.get("base_token") or "").strip()
        table_id = str(value.get("table_id") or "").strip()
        base_name = str(value.get("base_name") or "").strip()
        table_name = str(value.get("table_name") or "").strip()
        if not base_token or not table_id or not base_name or not table_name:
            raise ConfigError(
                "Feishu provisioning anchor requires Base/table tokens and names"
            )
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

    if intent == "feishu_provision_complete":
        if not isinstance(value, dict):
            raise ConfigError("Feishu provisioning result must be an object")
        mapping = value.get("field_mapping")
        if not isinstance(mapping, dict):
            raise ConfigError("Feishu field mapping must be an object")
        config["feishu"].update({"enabled": True, "field_mapping": deepcopy(mapping)})
        config["setup"]["execution_policy"]["allow_feishu_provisioning"] = False
        config["setup"]["execution_policy"]["provision_base_name"] = ""
        config["setup"]["execution_policy"]["provision_table_name"] = ""
        return config

    if intent == "feishu_mapping":
        if not isinstance(value, dict):
            raise ConfigError("Feishu field mapping must be an object")
        previous = deepcopy(config["feishu"])
        config["feishu"]["field_mapping"] = deepcopy(value)
        invalidate_for_feishu_change(config, previous, config["feishu"])
        return config

    if intent == "feishu_authorization":
        if not isinstance(value, dict):
            raise ConfigError("Feishu authorization transition must be an object")
        authorization_state = str(value.get("state") or "").strip()
        if not authorization_state:
            raise ConfigError("Feishu authorization state is required")
        now = datetime.now(timezone.utc).isoformat()
        authorization = _authorization(config)
        authorization["state"] = authorization_state
        authorization["identity"] = config["feishu"]["identity"]
        authorization["updated_at"] = now
        if bool(value.get("started")):
            authorization["started_at"] = now
        if bool(value.get("completed")):
            authorization["completed_at"] = now
        if authorization_state in {"waiting", "expired", "failed", "not_started"}:
            authorization["completed_at"] = ""
        state["authorization"] = deepcopy(authorization)
        return config

    if intent == "execution_policy":
        if not isinstance(value, dict):
            raise ConfigError("execution policy must be an object")
        config["setup"]["execution_policy"] = deepcopy(value)
        return config

    if intent == "feishu_disable":
        config["feishu"]["enabled"] = False
        config["setup"]["execution_policy"]["allow_feishu_sync"] = False
        return config

    if intent == "credentials_reset":
        config["redfox"] = {"api_key": ""}
        config["setup"]["feishu_identity_confirmed"] = False
        config["setup"]["feishu_authorization"] = deepcopy(
            DEFAULT_CONFIG["setup"]["feishu_authorization"]
        )
        config["setup"]["execution_policy"] = deepcopy(
            DEFAULT_CONFIG["setup"]["execution_policy"]
        )
        config["feishu"].update(
            {
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
            }
        )
        config["health"] = deepcopy(validate_config(DEFAULT_CONFIG)["health"])
        return config

    raise ConfigError(f"unsupported configuration intent: {intent}")


def transition(
    intent: str,
    value: Any = None,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Apply one domain intent and return its saved config plus transition facts."""
    state: dict[str, Any] = {}
    saved = modify_config(
        lambda config: _apply_domain_transition(config, intent, value, state),
        path=path,
    )
    state["config"] = saved
    if intent in {"feishu_authorization", "feishu_identity"} and "authorization" not in state:
        state["authorization"] = deepcopy(_authorization(saved))
    return state


def persist(
    intent: str,
    value: Any,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Normalize, validate, and atomically persist a domain configuration intent."""
    if intent == "agent_payload":
        try:
            return modify_config(
                lambda current: apply_agent_payload(value, existing=current), path=path
            )
        except ConfigError as exc:
            if "configuration not found" not in str(exc):
                raise
            config = apply_agent_payload(value)
            save_config(config, path=path)
            return config
    if intent.startswith("section:"):
        section = intent.removeprefix("section:")
        return modify_config(
            lambda current: apply_section_patch(current, section, value), path=path
        )
    return transition(intent, value, path=path)["config"]
