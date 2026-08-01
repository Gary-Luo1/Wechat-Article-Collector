"""Resolve lark-cli inside an isolated, path-stable application runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from paths import config_path, data_dir, secure_write_json


IDENTITY_ENV_KEYS = {
    "LARKSUITE_CLI_APP_ID",
    "LARKSUITE_CLI_APP_SECRET",
    "LARKSUITE_CLI_USER_ACCESS_TOKEN",
    "LARKSUITE_CLI_TENANT_ACCESS_TOKEN",
}
MAX_LARK_CONFIG_BYTES = 1024 * 1024
MAX_LARK_PROFILES = 100


def lark_cli_install_dir() -> Path:
    return data_dir() / "lark-cli"


def lark_cli_home_dir() -> Path:
    return (data_dir() / "lark-cli-home").resolve()


def lark_cli_config_dir() -> Path:
    # Keep the explicit CLI override and the CLI's HOME fallback on the same
    # private directory. This prevents a CLI release from falling back to the
    # user's real ~/.lark-cli configuration.
    return (lark_cli_home_dir() / ".lark-cli").resolve()


def lark_cli_work_dir() -> Path:
    return (data_dir() / "lark-cli-work").resolve()


def _explicit_cli_path() -> Path | None:
    raw = os.environ.get("WECHAT_LARK_CLI_PATH", "").strip().strip("\"'")
    return Path(raw).expanduser() if raw else None


def _native_candidates(path: Path) -> list[Path]:
    """Return native binary candidates associated with an npm launcher/path."""
    suffix = ".exe" if os.name == "nt" else ""
    names = [f"lark-cli{suffix}"]
    candidates: list[Path] = []
    if path.is_dir():
        candidates.extend(path / name for name in names)
        candidates.extend(path / "bin" / name for name in names)
        candidates.extend(
            path / "node_modules" / "@larksuite" / "cli" / "bin" / name
            for name in names
        )
        candidates.extend(
            path.parent / "@larksuite" / "cli" / "bin" / name for name in names
        )
    else:
        candidates.extend(
            path.parent / "node_modules" / "@larksuite" / "cli" / "bin" / name
            for name in names
        )
        if path.parent.name == ".bin":
            candidates.extend(
                path.parent.parent / "@larksuite" / "cli" / "bin" / name
                for name in names
            )
        if os.name != "nt" or path.suffix.casefold() == ".exe":
            candidates.append(path)
    return candidates


def _first_executable(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def resolve_lark_cli() -> Path:
    """Resolve the native binary first to avoid Windows .cmd encoding."""
    explicit = _explicit_cli_path()
    if explicit is not None:
        explicit_candidates = _native_candidates(explicit)
        selected = _first_executable(explicit_candidates)
        if selected is not None:
            return selected
        raise FileNotFoundError(
            "WECHAT_LARK_CLI_PATH does not point to a usable lark-cli executable: "
            + ", ".join(str(path) for path in explicit_candidates)
        )

    install = lark_cli_install_dir()
    candidates = _native_candidates(install)
    selected = _first_executable(candidates)
    if selected is not None:
        return selected

    discovered = shutil.which("lark-cli")
    if discovered:
        discovered_candidates = _native_candidates(Path(discovered))
        selected = _first_executable(discovered_candidates)
        if selected is not None:
            return selected

    checked = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        "lark-cli is not installed. Checked the explicit/isolated paths"
        + (f": {checked}" if checked else "")
    )


def _runtime_binding() -> dict[str, str]:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "app_id": "",
            "profile": "",
            "binding_mode": "",
            "agent_source": "",
        }
    feishu = raw.get("feishu") if isinstance(raw, dict) else None
    if not isinstance(feishu, dict):
        return {
            "app_id": "",
            "profile": "",
            "binding_mode": "",
            "agent_source": "",
        }
    return {
        "app_id": str(feishu.get("expected_app_id") or "").strip(),
        "profile": str(feishu.get("cli_profile") or "").strip(),
        "binding_mode": str(feishu.get("binding_mode") or "").strip(),
        "agent_source": str(feishu.get("agent_source") or "").strip(),
    }


def profile_name_for_app(app_id: str) -> str:
    normalized = app_id.strip()
    if not normalized:
        raise ValueError("Feishu App ID is required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"wechat-article-{digest}"


def safe_lark_arguments(arguments: list[str]) -> list[str]:
    """Pin operations to the Skill profile and reject profile-destructive calls."""
    args = list(arguments)
    if not args:
        return args
    if "--profile" in args:
        raise ValueError(
            "the Skill owns --profile selection; choose the App ID with "
            "manage feishu-app instead"
        )
    binding = _runtime_binding()
    command = args[0]
    subcommand = args[1] if len(args) > 1 else ""

    if command == "profile":
        if subcommand != "list":
            raise ValueError(
                "profile mutation is blocked; configure the selected app with "
                "manage feishu-app and lark config init --app-secret-stdin"
            )
        return args

    if command == "config" and subcommand == "init":
        if "--new" in args or "--force-init" in args:
            raise ValueError(
                "config init --new/--force-init is blocked because it can replace "
                "or create unrelated app configuration"
            )
        expected_app_id = binding["app_id"]
        profile = binding["profile"]
        if not expected_app_id or not profile:
            raise ValueError("run manage feishu-app --app-id <APP_ID> before config init")
        if "--app-secret-stdin" not in args:
            raise ValueError("config init must read the app secret with --app-secret-stdin")
        if "--app-id" not in args or args.index("--app-id") + 1 >= len(args):
            raise ValueError("config init requires the confirmed --app-id")
        if args[args.index("--app-id") + 1] != expected_app_id:
            raise ValueError("config init App ID does not match the confirmed Skill App ID")
        if "--name" in args:
            if args.index("--name") + 1 >= len(args):
                raise ValueError("config init --name requires a value")
            if args[args.index("--name") + 1] != profile:
                raise ValueError("config init profile does not match the Skill-owned profile")
        else:
            args.extend(["--name", profile])
        return args

    if command == "config" and subcommand in {
        "remove",
        "default-as",
        "strict-mode",
        "keychain-downgrade",
    }:
        raise ValueError(f"lark-cli config mutation is blocked for this Skill: {subcommand}")

    if command == "config" and subcommand == "bind":
        if binding["binding_mode"] != "agent":
            raise ValueError("config bind is allowed only for a confirmed Agent binding")
        for flag, expected in (
            ("--source", binding["agent_source"]),
            ("--app-id", binding["app_id"]),
        ):
            if not expected:
                raise ValueError(f"config bind requires a confirmed {flag} value")
            if flag not in args or args.index(flag) + 1 >= len(args):
                raise ValueError(f"config bind requires the confirmed {flag}")
            if args[args.index(flag) + 1] != expected:
                raise ValueError(f"config bind {flag} does not match the confirmed host context")
        return args

    if binding["profile"] and command not in {"update"} and "--profile" not in args:
        return ["--profile", binding["profile"], *args]
    return args


def global_lark_config_path() -> Path:
    return (Path.home() / ".lark-cli" / "config.json").resolve()


def global_lark_config_fingerprint() -> tuple[bool, int, int, str]:
    path = global_lark_config_path()
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        stat = path.stat()
    except OSError:
        return (False, 0, 0, "")
    return (
        True,
        stat.st_size,
        stat.st_mtime_ns,
        digest.hexdigest(),
    )


def _read_lark_config(path: Path) -> dict[str, Any]:
    """Read one bounded lark-cli config without returning secret values."""
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"lark-cli configuration was not found at {path}") from exc
    if not path.is_file():
        raise ValueError(f"lark-cli configuration is not a regular file: {path}")
    if stat.st_size > MAX_LARK_CONFIG_BYTES:
        raise ValueError(
            f"lark-cli configuration exceeds the {MAX_LARK_CONFIG_BYTES}-byte safety limit"
        )
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_LARK_CONFIG_BYTES + 1)
        if len(data) > MAX_LARK_CONFIG_BYTES:
            raise ValueError(
                f"lark-cli configuration exceeds the {MAX_LARK_CONFIG_BYTES}-byte "
                "safety limit"
            )
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
    if len(apps) > MAX_LARK_PROFILES:
        raise ValueError(
            f"lark-cli configuration contains more than {MAX_LARK_PROFILES} profiles"
        )
    if not all(isinstance(item, dict) for item in apps):
        raise ValueError("every lark-cli profile must be an object")
    return payload


def _secret_storage(profile: dict[str, Any]) -> str:
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


def _metadata_text(value: Any, limit: int = 128) -> str:
    """Bound untrusted profile labels before returning them to an Agent."""
    text = str(value or "").strip()
    return "".join(character for character in text if ord(character) >= 32)[:limit]


def discover_global_lark_profiles() -> dict[str, Any]:
    """Return redacted metadata for the user's existing lark-cli profiles.

    This function never invokes lark-cli and never returns an App Secret, keychain
    identifier, access token, or user Open ID.
    """
    path = global_lark_config_path()
    before = global_lark_config_fingerprint()
    if not before[0]:
        return {
            "exists": False,
            "path": str(path),
            "profile_count": 0,
            "profiles": [],
            "secrets_included": False,
            "config_unchanged": True,
        }
    payload = _read_lark_config(path)
    profiles: list[dict[str, Any]] = []
    for item in payload["apps"]:
        users = item.get("users")
        storage = _secret_storage(item)
        profiles.append(
            {
                "name": _metadata_text(item.get("name")),
                "app_id": _metadata_text(item.get("appId")),
                "brand": _metadata_text(item.get("brand"), 32),
                "default_as": _metadata_text(item.get("defaultAs"), 32),
                "strict_mode": _metadata_text(item.get("strictMode"), 32),
                "app_secret_available": storage in {"inline", "keychain"},
                "app_secret_storage": storage,
                "authorized_user_count": len(users) if isinstance(users, list) else 0,
            }
        )
    after = global_lark_config_fingerprint()
    if after != before:
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


def import_global_lark_profile(expected_app_id: str, target_profile: str) -> dict[str, Any]:
    """Clone one app credential into isolated state without modifying the source.

    User authorization entries are intentionally excluded because token refreshes
    can mutate shared keychain state. User identity must authorize once inside the
    isolated profile; bot identity can immediately reuse the copied App credential.
    """
    app_id = expected_app_id.strip()
    profile_name = target_profile.strip()
    app_id_suffix = app_id[4:] if app_id.startswith("cli_") else ""
    if (
        not app_id_suffix
        or not app_id_suffix.isascii()
        or not app_id_suffix.isalnum()
    ):
        raise ValueError(
            "the selected Feishu App ID must start with cli_ and contain only "
            "ASCII letters/digits"
        )
    if (
        not profile_name
        or len(profile_name) > 128
        or any(ord(character) < 32 for character in profile_name)
    ):
        raise ValueError("the isolated lark-cli profile name is invalid")

    source_path = global_lark_config_path()
    source_before = global_lark_config_fingerprint()
    source = _read_lark_config(source_path)
    matches = [
        item
        for item in source["apps"]
        if str(item.get("appId") or "").strip() == app_id
    ]
    if not matches:
        raise ValueError(
            f"no existing local lark-cli profile matches the selected App ID {app_id}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"multiple existing local lark-cli profiles match App ID {app_id}; "
            "resolve the duplicate before importing"
        )
    selected = matches[0]
    storage = _secret_storage(selected)
    if storage not in {"inline", "keychain"}:
        raise ValueError(
            "the selected local profile does not expose a reusable inline/keychain "
            "App credential; configure the isolated profile through secret stdin"
        )
    if global_lark_config_fingerprint() != source_before:
        raise RuntimeError(
            "the user's lark-cli configuration changed during import inspection; retry"
        )

    private_path = lark_cli_config_dir() / "config.json"
    if private_path.exists():
        private = _read_lark_config(private_path)
    else:
        private = {"apps": []}
    private_apps = private["apps"]
    named = [
        item
        for item in private_apps
        if str(item.get("name") or "").strip() == profile_name
    ]
    if named:
        if (
            len(named) == 1
            and str(named[0].get("appId") or "").strip() == app_id
            and _secret_storage(named[0]) != "missing"
        ):
            return {
                "imported": False,
                "already_configured": True,
                "app_id": app_id,
                "private_profile": profile_name,
                "source_config_unchanged": global_lark_config_fingerprint()
                == source_before,
                "user_tokens_imported": False,
                "secrets_included": False,
            }
        raise ValueError(
            f"isolated lark-cli profile name {profile_name!r} is already in use"
        )
    duplicates = [
        item
        for item in private_apps
        if str(item.get("appId") or "").strip() == app_id
    ]
    if duplicates:
        raise ValueError(
            f"the selected App ID {app_id} already exists under another isolated "
            "profile; refusing to create an ambiguous duplicate"
        )

    imported = {
        key: deepcopy(selected[key])
        for key in ("appId", "appSecret", "brand", "lang", "defaultAs", "strictMode")
        if key in selected
    }
    imported.update({"name": profile_name, "users": []})
    private_apps.append(imported)
    if global_lark_config_fingerprint() != source_before:
        raise RuntimeError(
            "the user's lark-cli configuration changed before the isolated copy was "
            "written; retry"
        )
    secure_write_json(private_path, private)
    source_unchanged = global_lark_config_fingerprint() == source_before
    if not source_unchanged:
        raise RuntimeError(
            "the user's lark-cli configuration changed concurrently; inspect both "
            "configurations before continuing"
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


def lark_cli_environment() -> dict[str, str]:
    home = lark_cli_home_dir()
    config = lark_cli_config_dir()
    environment: dict[str, Any] = dict(os.environ)
    for key in IDENTITY_ENV_KEYS:
        environment.pop(key, None)
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LARKSUITE_CLI_CONFIG_DIR": str(config),
            "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
            "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
        }
    )
    return {str(key): str(value) for key, value in environment.items()}
