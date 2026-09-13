"""Read-only inspection and safe copying of user lark-cli profiles.

The command executor does not need to know how profiles are discovered or
copied.  This module keeps those filesystem rules together and exposes only
redacted metadata to setup callers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

MAX_CONFIG_BYTES = 1024 * 1024
MAX_PROFILES = 100


def profile_name_for_app(app_id: str) -> str:
    normalized = app_id.strip()
    if not normalized:
        raise ValueError("Feishu App ID is required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"wechat-article-{digest}"


def config_path() -> Path:
    return (Path.home() / ".lark-cli" / "config.json").resolve()


def fingerprint(path: Path) -> tuple[bool, int, int, str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        stat = path.stat()
    except OSError:
        return (False, 0, 0, "")
    return (True, stat.st_size, stat.st_mtime_ns, digest.hexdigest())


def read_config(path: Path) -> dict[str, Any]:
    """Read one bounded lark-cli config without returning secret values."""
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"lark-cli configuration was not found at {path}") from exc
    if not path.is_file():
        raise ValueError(f"lark-cli configuration is not a regular file: {path}")
    if stat.st_size > MAX_CONFIG_BYTES:
        raise ValueError(f"lark-cli configuration exceeds the {MAX_CONFIG_BYTES}-byte safety limit")
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError(f"lark-cli configuration exceeds the {MAX_CONFIG_BYTES}-byte safety limit")
        payload = json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"cannot read lark-cli configuration metadata: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("lark-cli configuration root must be an object")
    apps = payload.get("apps")
    if not isinstance(apps, list):
        raise ValueError("lark-cli configuration must contain an apps list")
    if len(apps) > MAX_PROFILES:
        raise ValueError(f"lark-cli configuration contains more than {MAX_PROFILES} profiles")
    if not all(isinstance(item, dict) for item in apps):
        raise ValueError("every lark-cli profile must be an object")
    return payload


def secret_storage(profile: dict[str, Any]) -> str:
    secret = profile.get("appSecret")
    if isinstance(secret, str):
        return "inline" if secret else "missing"
    if isinstance(secret, dict):
        source = str(secret.get("source") or "").strip().casefold()
        identifier = str(secret.get("id") or "").strip()
        if not source or not identifier:
            return "missing"
        return source if source == "keychain" else "unsupported"
    return "missing"


def metadata_text(value: Any, limit: int = 128) -> str:
    """Bound untrusted profile labels before returning them to an Agent."""
    text = str(value or "").strip()
    return "".join(character for character in text if ord(character) >= 32)[:limit]


def discover(
    path: Path,
    *,
    fingerprint_fn: Callable[[], tuple[bool, int, int, str]],
) -> dict[str, Any]:
    """Return redacted profile metadata and reject concurrent source changes."""
    before = fingerprint_fn()
    if not before[0]:
        return {
            "exists": False,
            "path": str(path),
            "profile_count": 0,
            "profiles": [],
            "secrets_included": False,
            "config_unchanged": True,
        }
    payload = read_config(path)
    profiles: list[dict[str, Any]] = []
    for item in payload["apps"]:
        users = item.get("users")
        storage = secret_storage(item)
        profiles.append(
            {
                "name": metadata_text(item.get("name")),
                "app_id": metadata_text(item.get("appId")),
                "brand": metadata_text(item.get("brand"), 32),
                "default_as": metadata_text(item.get("defaultAs"), 32),
                "strict_mode": metadata_text(item.get("strictMode"), 32),
                "app_secret_available": storage in {"inline", "keychain"},
                "app_secret_storage": storage,
                "authorized_user_count": len(users) if isinstance(users, list) else 0,
            }
        )
    if fingerprint_fn() != before:
        raise RuntimeError(
            "the user's lark-cli configuration changed while it was being inspected; "
            "retry after other lark-cli activity finishes"
        )
    return {
        "exists": True,
        "path": str(path),
        "profile_count": len(profiles),
        "profiles": profiles,
        "secrets_included": False,
        "config_unchanged": True,
    }


def private_secret_state(
    binding: dict[str, str],
    private_dir: Path,
) -> dict[str, Any]:
    """Return local-only readiness for the bound isolated profile."""
    profile = binding["profile"]
    result: dict[str, Any] = {
        "bound": bool(profile),
        "profile": profile,
        "app_secret_storage": "missing" if profile else "unbound",
        "ready": False,
    }
    if not profile:
        return result
    try:
        payload = read_config(private_dir / "config.json")
    except (FileNotFoundError, ValueError, OSError):
        return result
    for item in payload["apps"]:
        if str(item.get("name") or "").strip() != profile:
            continue
        storage = secret_storage(item)
        result["app_secret_storage"] = storage
        result["ready"] = storage in {"inline", "keychain"}
        break
    return result


def import_profile(
    expected_app_id: str,
    target_profile: str,
    *,
    source_path: Path,
    private_dir: Path,
    fingerprint_fn: Callable[[], tuple[bool, int, int, str]],
    secure_write: Callable[[Path, dict[str, Any]], Any],
) -> dict[str, Any]:
    """Clone only one App credential into isolated state, leaving source intact."""
    app_id = expected_app_id.strip()
    profile_name = target_profile.strip()
    app_id_suffix = app_id[4:] if app_id.startswith("cli_") else ""
    if not app_id_suffix or not app_id_suffix.isascii() or not app_id_suffix.isalnum():
        raise ValueError(
            "the selected Feishu App ID must start with cli_ and contain only ASCII letters/digits"
        )
    if (
        not profile_name
        or len(profile_name) > 128
        or any(ord(character) < 32 for character in profile_name)
    ):
        raise ValueError("the isolated lark-cli profile name is invalid")

    source_before = fingerprint_fn()
    source = read_config(source_path)
    matches = [
        item for item in source["apps"] if str(item.get("appId") or "").strip() == app_id
    ]
    if not matches:
        raise ValueError(f"no existing local lark-cli profile matches the selected App ID {app_id}")
    if len(matches) > 1:
        raise ValueError(
            f"multiple existing local lark-cli profiles match App ID {app_id}; resolve the duplicate before importing"
        )
    selected = matches[0]
    storage = secret_storage(selected)
    if storage not in {"inline", "keychain"}:
        raise ValueError(
            "the selected local profile does not expose a reusable inline/keychain App credential; "
            "configure the isolated profile through secret stdin"
        )
    if fingerprint_fn() != source_before:
        raise RuntimeError("the user's lark-cli configuration changed during import inspection; retry")

    private_path = private_dir / "config.json"
    private = read_config(private_path) if private_path.exists() else {"apps": []}
    private_apps = private["apps"]
    named = [item for item in private_apps if str(item.get("name") or "").strip() == profile_name]
    if named:
        if (
            len(named) == 1
            and str(named[0].get("appId") or "").strip() == app_id
            and secret_storage(named[0]) != "missing"
        ):
            return {
                "imported": False,
                "already_configured": True,
                "app_id": app_id,
                "private_profile": profile_name,
                "source_config_unchanged": fingerprint_fn() == source_before,
                "user_tokens_imported": False,
                "secrets_included": False,
            }
        raise ValueError(f"isolated lark-cli profile name {profile_name!r} is already in use")
    if any(str(item.get("appId") or "").strip() == app_id for item in private_apps):
        raise ValueError(
            f"the selected App ID {app_id} already exists under another isolated profile; refusing to create an ambiguous duplicate"
        )

    imported = {
        key: deepcopy(selected[key])
        for key in ("appId", "appSecret", "brand", "lang", "defaultAs", "strictMode")
        if key in selected
    }
    imported.update({"name": profile_name, "users": []})
    private_apps.append(imported)
    if fingerprint_fn() != source_before:
        raise RuntimeError(
            "the user's lark-cli configuration changed before the isolated copy was written; retry"
        )
    secure_write(private_path, private)
    if fingerprint_fn() != source_before:
        raise RuntimeError(
            "the user's lark-cli configuration changed concurrently; inspect both configurations before continuing"
        )
    return {
        "imported": True,
        "already_configured": False,
        "app_id": app_id,
        "private_profile": profile_name,
        "app_secret_storage": storage,
        "source_config_unchanged": True,
        "user_tokens_imported": False,
        "secrets_included": False,
    }
