"""Configuration loading, validation, and secure persistence."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from paths import config_path, data_dir, secure_write_json
from process_lock import process_lock


CONFIG_VERSION = 10
DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "setup": {
        "search_window_confirmed": False,
        "feishu_identity_confirmed": False,
        "feishu_authorization": {
            "state": "not_started",
            "identity": "",
            "started_at": "",
            "completed_at": "",
            "updated_at": "",
        },
        "execution_policy": {
            "confirmed": False,
            "mode": "guided",
            "unlisted_publisher": "ask",
            "allow_feishu_provisioning": False,
            "provision_base_name": "",
            "provision_table_name": "",
            "allow_feishu_sync": False,
            "approved_at": "",
            "scope_version": 1,
        },
    },
    "wechat": {"cookie": "", "token": ""},
    "subscriptions": [],
    "feishu": {
        "destination": "undecided",
        "enabled": False,
        "identity": "user",
        "binding_mode": "",
        "agent_source": "",
        "expected_app_id": "",
        "cli_profile": "",
        "expected_user_open_id": "",
        "manager_open_id": "",
        "base_token": "",
        "table_id": "",
        "provisioning": "",
        "created_base_name": "",
        "created_table_name": "",
        "schema_policy": "mapped",
        "field_mapping": {},
    },
    "settings": {
        "check_hours": 24,
        "request_delay": 3.0,
        "max_articles_per_account": 10,
        # URL identity is authoritative. Optional content deduplication is off by
        # default because distinct articles may legitimately reuse titles and
        # summaries.
        "content_dedup": False,
        "min_score": 6.0,
        "output_language": "auto",
    },
    "preferences": {
        "include_topics": [],
        "exclude_keywords": [],
        "preferred_accounts": [],
        "digest_hours": 24,
        "digest_limit": 5,
    },
    "health": {
        "wechat": {
            "last_verified_at": "",
            "last_failure_kind": "",
            "consecutive_failures": 0,
        },
        "subscriptions": {"last_verified_at": "", "unresolved": 0},
        "feishu": {
            "last_verified_at": "",
            "last_failure_kind": "",
            "consecutive_failures": 0,
        },
    },
}


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


FEISHU_FIELD_KEYS = {
    "title",
    "account",
    "account_id",
    "url",
    "summary",
    "published_at",
    "fetched_at",
    "score",
    "rationale",
    "tags",
    "read_status",
}

LEGACY_FIELD_MAPPING = {
    "title": {"name": "文章标题"},
    "account": {"name": "公众号名称"},
    "account_id": {"name": "公众号ID"},
    "url": {"name": "文章链接"},
    "summary": {"name": "文章摘要"},
    "published_at": {"name": "发布日期"},
    "fetched_at": {"name": "抓取时间"},
    "score": {"name": "AI评分"},
    "rationale": {"name": "评分理由"},
    "tags": {"name": "文章标签"},
    "read_status": {"name": "阅读状态"},
}


def _merge_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_CONFIG)
    merged["version"] = raw.get("version", 1)
    for section in ("wechat", "feishu", "settings", "preferences"):
        value = raw.get(section, {})
        if isinstance(value, dict):
            merged[section].update(value)
    raw_setup = raw.get("setup", {})
    if isinstance(raw_setup, dict):
        nested_setup = {"feishu_authorization", "execution_policy"}
        merged["setup"].update(
            {key: value for key, value in raw_setup.items() if key not in nested_setup}
        )
        for key in nested_setup:
            raw_value = raw_setup.get(key)
            if isinstance(raw_value, dict):
                merged["setup"][key].update(raw_value)
            elif key in raw_setup:
                merged["setup"][key] = raw_value
    if "subscriptions" in raw:
        merged["subscriptions"] = raw["subscriptions"]
    raw_health = raw.get("health")
    if isinstance(raw_health, dict):
        for section in merged["health"]:
            value = raw_health.get(section)
            if isinstance(value, dict):
                merged["health"][section].update(value)
    # Migrate the pre-mapping format without forcing existing installations to
    # expose WeChat credentials again. The names are resolved to real field IDs
    # by the Feishu preflight before a write.
    raw_feishu = raw.get("feishu")
    if isinstance(raw_feishu, dict):
        has_target = bool(str(merged["feishu"].get("base_token", "")).strip()) and bool(
            str(merged["feishu"].get("table_id", "")).strip()
        )
        if "enabled" not in raw_feishu:
            merged["feishu"]["enabled"] = has_target
        if has_target and "field_mapping" not in raw_feishu:
            merged["feishu"]["field_mapping"] = deepcopy(LEGACY_FIELD_MAPPING)
            merged["feishu"]["provisioning"] = "existing"
        if "destination" not in raw_feishu:
            policy = merged["setup"]["execution_policy"]
            if has_target:
                merged["feishu"]["destination"] = (
                    "create"
                    if merged["feishu"].get("provisioning") == "created"
                    else "existing"
                )
            elif bool(policy.get("allow_feishu_provisioning")):
                merged["feishu"]["destination"] = "create"
            elif bool(policy.get("allow_feishu_sync")):
                merged["feishu"]["destination"] = "existing"
            elif bool(policy.get("confirmed")):
                # Older confirmed policies always included the two Feishu
                # allow/deny decisions, so deny+deny is a recoverable explicit
                # skip. Unconfirmed WeChat-only installs remain undecided and
                # must answer the question once after upgrading.
                merged["feishu"]["destination"] = "skip"
            elif bool(merged["feishu"].get("enabled")):
                merged["feishu"]["destination"] = "existing"
    return merged


def _validate_health(health: Any) -> None:
    if not isinstance(health, dict):
        raise ConfigError("health must be an object")
    for section in ("wechat", "subscriptions", "feishu"):
        if not isinstance(health.get(section), dict):
            raise ConfigError(f"health.{section} must be an object")
    for section in ("wechat", "feishu"):
        value = health[section]
        for key in ("last_verified_at", "last_failure_kind"):
            if not isinstance(value.get(key), str):
                raise ConfigError(f"health.{section}.{key} must be a string")
        failures = value.get("consecutive_failures")
        if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
            raise ConfigError(f"health.{section}.consecutive_failures must be non-negative")
    subscriptions = health["subscriptions"]
    if not isinstance(subscriptions.get("last_verified_at"), str):
        raise ConfigError("health.subscriptions.last_verified_at must be a string")
    unresolved = subscriptions.get("unresolved")
    if not isinstance(unresolved, int) or isinstance(unresolved, bool) or unresolved < 0:
        raise ConfigError("health.subscriptions.unresolved must be non-negative")


def _validate_feishu(feishu: Any) -> None:
    if not isinstance(feishu, dict):
        raise ConfigError("feishu must be an object")
    if feishu.get("destination") not in {"undecided", "skip", "existing", "create"}:
        raise ConfigError(
            "feishu.destination must be undecided, skip, existing, or create"
        )
    if not isinstance(feishu.get("enabled"), bool):
        raise ConfigError("feishu.enabled must be boolean")
    if feishu.get("identity") not in {"user", "bot"}:
        raise ConfigError("feishu.identity must be user or bot")
    for key in (
        "binding_mode",
        "agent_source",
        "expected_app_id",
        "cli_profile",
        "expected_user_open_id",
        "manager_open_id",
        "base_token",
        "table_id",
        "provisioning",
        "schema_policy",
    ):
        if not isinstance(feishu.get(key), str):
            raise ConfigError(f"feishu.{key} must be a string")
    if feishu["binding_mode"] not in {"", "agent", "existing", "dedicated"}:
        raise ConfigError(
            "feishu.binding_mode must be agent, existing, dedicated, or empty"
        )
    if feishu["agent_source"] not in {"", "openclaw", "hermes", "lark-channel"}:
        raise ConfigError(
            "feishu.agent_source must be openclaw, hermes, lark-channel, or empty"
        )
    if feishu["binding_mode"] == "agent" and not feishu["agent_source"]:
        raise ConfigError("feishu.agent_source is required for agent binding")
    if feishu["binding_mode"] != "agent" and feishu["agent_source"]:
        raise ConfigError("feishu.agent_source is only valid for agent binding")
    if feishu["provisioning"] not in {"", "created", "existing"}:
        raise ConfigError("feishu.provisioning must be created, existing, or empty")
    if feishu["schema_policy"] not in {"mapped", "extend_confirmed"}:
        raise ConfigError("feishu.schema_policy must be mapped or extend_confirmed")
    base_token = feishu["base_token"].strip()
    table_id = feishu["table_id"].strip()
    if bool(base_token) != bool(table_id):
        raise ConfigError("feishu.base_token and feishu.table_id must be provided together")
    if feishu["enabled"] and not (base_token and table_id):
        raise ConfigError("enabled Feishu sync requires base_token and table_id")
    if feishu["enabled"] and feishu["destination"] not in {"existing", "create"}:
        raise ConfigError(
            "enabled Feishu sync requires destination=existing or destination=create"
        )
    mapping = feishu.get("field_mapping")
    if not isinstance(mapping, dict):
        raise ConfigError("feishu.field_mapping must be an object")
    unexpected = set(mapping) - FEISHU_FIELD_KEYS
    if unexpected:
        raise ConfigError(f"unsupported Feishu field mappings: {sorted(unexpected)}")
    for logical_name, target in mapping.items():
        if not isinstance(target, dict):
            raise ConfigError(f"feishu.field_mapping.{logical_name} must be an object")
        extra = set(target) - {"field_id", "name", "type"}
        if extra:
            raise ConfigError(
                f"feishu.field_mapping.{logical_name} contains unsupported keys: {sorted(extra)}"
            )
        for key in ("field_id", "name", "type"):
            if key in target and not isinstance(target[key], (str, int)):
                raise ConfigError(f"feishu.field_mapping.{logical_name}.{key} is invalid")
        if not str(target.get("field_id", "")).strip() and not str(
            target.get("name", "")
        ).strip():
            raise ConfigError(
                f"feishu.field_mapping.{logical_name} needs field_id or name"
            )
    # An enabled target may temporarily have an empty mapping while the Agent
    # performs read-only field discovery. preflight_feishu is the readiness
    # boundary and requires resolvable title/url fields before any write.


def validate_config(config: dict[str, Any], *, require_wechat: bool = False) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigError("config must be a JSON object")
    config = _merge_defaults(config)
    version = config.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigError("config.version must be an integer")
    if version > CONFIG_VERSION:
        raise ConfigError(
            f"configuration version {version} is newer than supported version {CONFIG_VERSION}"
        )
    config["version"] = CONFIG_VERSION
    setup = config["setup"]
    if not isinstance(setup.get("search_window_confirmed"), bool):
        raise ConfigError("setup.search_window_confirmed must be boolean")
    if not isinstance(setup.get("feishu_identity_confirmed"), bool):
        raise ConfigError("setup.feishu_identity_confirmed must be boolean")
    authorization = setup.get("feishu_authorization")
    if not isinstance(authorization, dict):
        raise ConfigError("setup.feishu_authorization must be an object")
    if authorization.get("state") not in {
        "not_started",
        "waiting",
        "authorized",
        "expired",
        "failed",
        "not_required",
    }:
        raise ConfigError("setup.feishu_authorization.state is invalid")
    if authorization.get("identity") not in {"", "user", "bot"}:
        raise ConfigError("setup.feishu_authorization.identity must be user, bot, or empty")
    if authorization["state"] == "not_required" and authorization["identity"] != "bot":
        raise ConfigError("not_required Feishu authorization must use bot identity")
    if authorization["state"] in {"waiting", "authorized", "expired", "failed"} and authorization["identity"] != "user":
        raise ConfigError(
            f"{authorization['state']} Feishu authorization must use user identity"
        )
    for key in ("started_at", "completed_at", "updated_at"):
        if not isinstance(authorization.get(key), str):
            raise ConfigError(f"setup.feishu_authorization.{key} must be a string")
    execution_policy = setup.get("execution_policy")
    if not isinstance(execution_policy, dict):
        raise ConfigError("setup.execution_policy must be an object")
    if not isinstance(execution_policy.get("confirmed"), bool):
        raise ConfigError("setup.execution_policy.confirmed must be boolean")
    if execution_policy.get("mode") not in {"guided", "autopilot"}:
        raise ConfigError("setup.execution_policy.mode must be guided or autopilot")
    if execution_policy.get("unlisted_publisher") not in {
        "ask",
        "ingest_once",
        "auto_subscribe",
    }:
        raise ConfigError(
            "setup.execution_policy.unlisted_publisher must be ask, ingest_once, "
            "or auto_subscribe"
        )
    for key in ("allow_feishu_provisioning", "allow_feishu_sync"):
        if not isinstance(execution_policy.get(key), bool):
            raise ConfigError(f"setup.execution_policy.{key} must be boolean")
    for key in ("provision_base_name", "provision_table_name", "approved_at"):
        if not isinstance(execution_policy.get(key), str):
            raise ConfigError(f"setup.execution_policy.{key} must be a string")
    scope_version = execution_policy.get("scope_version")
    if not isinstance(scope_version, int) or isinstance(scope_version, bool):
        raise ConfigError("setup.execution_policy.scope_version must be an integer")
    if scope_version != 1:
        raise ConfigError("setup.execution_policy.scope_version is unsupported")
    if execution_policy["mode"] == "guided" and (
        execution_policy["unlisted_publisher"] != "ask"
        or execution_policy["allow_feishu_provisioning"]
        or execution_policy["allow_feishu_sync"]
    ):
        raise ConfigError(
            "guided execution policy cannot pre-authorize subscription or Feishu writes"
        )
    if execution_policy["allow_feishu_provisioning"] and (
        not execution_policy["provision_base_name"].strip()
        or not execution_policy["provision_table_name"].strip()
    ):
        raise ConfigError(
            "Feishu provisioning approval requires provision_base_name and "
            "provision_table_name"
        )
    if not execution_policy["allow_feishu_provisioning"] and (
        execution_policy["provision_base_name"].strip()
        or execution_policy["provision_table_name"].strip()
    ):
        raise ConfigError(
            "Feishu provisioning names require allow_feishu_provisioning=true"
        )
    wechat = config["wechat"]
    if not isinstance(wechat.get("cookie"), str) or not isinstance(wechat.get("token"), str):
        raise ConfigError("wechat.cookie and wechat.token must be strings")
    if require_wechat and (not wechat["cookie"].strip() or not wechat["token"].strip()):
        raise ConfigError("WeChat cookie/token are missing; run setup locally")
    subscriptions = config["subscriptions"]
    if not isinstance(subscriptions, list):
        raise ConfigError("subscriptions must be a list")
    for index, subscription in enumerate(subscriptions):
        if not isinstance(subscription, dict):
            raise ConfigError(f"subscriptions[{index}] must be an object")
        if not any(str(subscription.get(key, "")).strip() for key in ("name", "alias", "biz")):
            raise ConfigError(f"subscriptions[{index}] needs name, alias, or biz")
    settings = config["settings"]
    numeric_rules = {
        "check_hours": (1, 24 * 365),
        "request_delay": (0, 60),
        "max_articles_per_account": (1, 100),
        "min_score": (1, 10),
    }
    for key, (minimum, maximum) in numeric_rules.items():
        value = settings.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigError(f"settings.{key} must be numeric")
        if not minimum <= value <= maximum:
            raise ConfigError(f"settings.{key} must be between {minimum} and {maximum}")
    if not isinstance(settings.get("content_dedup"), bool):
        raise ConfigError("settings.content_dedup must be boolean")
    if settings.get("output_language") not in {"auto", "zh", "en"}:
        raise ConfigError("settings.output_language must be auto, zh, or en")
    preferences = config["preferences"]
    if not isinstance(preferences, dict):
        raise ConfigError("preferences must be an object")
    for key in ("include_topics", "exclude_keywords", "preferred_accounts"):
        values = preferences.get(key)
        if not isinstance(values, list) or len(values) > 100:
            raise ConfigError(f"preferences.{key} must be a list with at most 100 items")
        for index, value in enumerate(values):
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"preferences.{key}[{index}] must be a non-empty string")
    digest_hours = preferences.get("digest_hours")
    if not isinstance(digest_hours, int) or isinstance(digest_hours, bool) or not 1 <= digest_hours <= 8760:
        raise ConfigError("preferences.digest_hours must be an integer between 1 and 8760")
    digest_limit = preferences.get("digest_limit")
    if not isinstance(digest_limit, int) or isinstance(digest_limit, bool) or not 1 <= digest_limit <= 50:
        raise ConfigError("preferences.digest_limit must be an integer between 1 and 50")
    _validate_feishu(config["feishu"])
    destination = config["feishu"]["destination"]
    if execution_policy["confirmed"] and destination == "undecided":
        raise ConfigError(
            "confirmed execution policy requires an explicit Feishu destination"
        )
    if destination == "skip" and (
        execution_policy["allow_feishu_provisioning"]
        or execution_policy["allow_feishu_sync"]
    ):
        raise ConfigError(
            "destination=skip cannot allow Feishu provisioning or sync"
        )
    if destination == "existing" and execution_policy["allow_feishu_provisioning"]:
        raise ConfigError(
            "destination=existing cannot allow Feishu Base provisioning"
        )
    _validate_health(config["health"])
    return config


def load_config(path: Path | None = None, *, require_wechat: bool = False) -> dict[str, Any]:
    target = Path(path) if path else config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found at {target}; run setup locally") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read configuration at {target}: {exc}") from exc
    return validate_config(raw, require_wechat=require_wechat)


def save_config(config: dict[str, Any], path: Path | None = None) -> Path:
    target = Path(path) if path else config_path()
    validated = validate_config(config)
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8-sig"))
            old_version = existing.get("version", 1) if isinstance(existing, dict) else 1
            if isinstance(old_version, int) and old_version < CONFIG_VERSION:
                backup = target.with_name(f"config.v{old_version}.backup.json")
                if not backup.exists():
                    secure_write_json(backup, existing)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    secure_write_json(target, validated)
    return target


@contextmanager
def config_lock(timeout: float = 10.0) -> Iterator[None]:
    """Acquire a cross-platform process lock for configuration transactions."""
    with process_lock(data_dir() / "config.lock", timeout=timeout):
        yield


def modify_config(
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
    path: Path | None = None,
) -> dict[str, Any]:
    """Apply a read-modify-write transaction to the configuration under lock.

    The mutator receives the latest validated configuration, mutates it in
    place, and returns it (returning ``None`` is accepted for in-place
    mutation). The result is validated and saved before the lock is released.
    If the mutator raises or validation fails, nothing is written.
    """
    with config_lock():
        config = load_config(path)
        result = mutator(config)
        result = result if result is not None else config
        save_config(result, path)
        return validate_config(result)


def update_health(
    section: str,
    *,
    success: bool,
    failure_kind: str = "",
    path: Path | None = None,
    unresolved: int | None = None,
) -> dict[str, Any]:
    if section not in {"wechat", "subscriptions", "feishu"}:
        raise ConfigError(f"unsupported health section: {section}")
    now = datetime.now(timezone.utc).isoformat()

    def mutate(config: dict[str, Any]) -> dict[str, Any]:
        health = config["health"][section]
        if section == "subscriptions":
            health["last_verified_at"] = now
            if unresolved is not None:
                health["unresolved"] = max(0, int(unresolved))
        elif success:
            health["last_verified_at"] = now
            health["last_failure_kind"] = ""
            health["consecutive_failures"] = 0
        else:
            health["last_failure_kind"] = failure_kind[:100]
            health["consecutive_failures"] = int(health["consecutive_failures"]) + 1
        return config

    return modify_config(mutate, path=path)


def redacted_config(config: dict[str, Any]) -> dict[str, Any]:
    validated = validate_config(config)
    return {
        "version": validated["version"],
        "setup": deepcopy(validated["setup"]),
        "wechat": {
            "configured": bool(
                validated["wechat"]["cookie"].strip()
                and validated["wechat"]["token"].strip()
            )
        },
        "subscriptions": deepcopy(validated["subscriptions"]),
        "feishu": {
            "destination": validated["feishu"]["destination"],
            "enabled": validated["feishu"]["enabled"],
            "identity": validated["feishu"]["identity"],
            "binding_mode": validated["feishu"]["binding_mode"],
            "agent_source": validated["feishu"]["agent_source"],
            "expected_app_id": validated["feishu"]["expected_app_id"],
            "cli_profile_configured": bool(validated["feishu"]["cli_profile"]),
            "expected_user_configured": bool(
                validated["feishu"]["expected_user_open_id"]
            ),
            "manager_configured": bool(validated["feishu"]["manager_open_id"]),
            "target_configured": bool(
                validated["feishu"]["base_token"]
                and validated["feishu"]["table_id"]
            ),
            "provisioning": validated["feishu"]["provisioning"],
            "schema_policy": validated["feishu"]["schema_policy"],
            "field_mapping": deepcopy(validated["feishu"]["field_mapping"]),
        },
        "settings": deepcopy(validated["settings"]),
        "preferences": deepcopy(validated["preferences"]),
        "health": deepcopy(validated["health"]),
    }
