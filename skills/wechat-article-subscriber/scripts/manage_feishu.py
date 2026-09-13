"""Feishu onboarding and identity handlers for the manage command."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from bitable_client import (
    create_standard_base,
    created_base_identifiers,
    feishu_identity_context,
    grant_bot_created_resource,
    list_fields,
    preflight_feishu,
    probe_app_secret_resolution,
    resolve_lark_profile,
    standard_field_schema,
    verify_feishu_identity,
)
from config_store import (
    ConfigError,
    load_config,
    update_health,
)
from config_transitions import transition
from execution_policy import (
    allows_automatic_provisioning,
    policy_for,
)
from feishu_setup import (
    bind_agent_context,
    choose_app,
    choose_destination,
    choose_identity,
    detect_agent_source,
    save_authorization_state,
    setup_status,
    set_manager,
)
from lark_runtime import (
    LarkCLIError,
    discover_global_lark_profiles,
    import_global_lark_profile,
    private_profile_secret_state,
    run_lark,
)
from paths import data_dir, open_with_default_app
from protocol import _read_secret_stdin

SECRET_FILE_NAME = "feishu-app-secret.txt"
SECRET_FILE_PLACEHOLDER = "PASTE_APP_SECRET_HERE"
SECRET_FILE_MAX_BYTES = 64 * 1024


def _authorization(config: dict[str, Any]) -> dict[str, Any]:
    return config["setup"]["feishu_authorization"]


def _expected_app_id(config: dict[str, Any]) -> str:
    """Return the saved Feishu App ID, normalized."""
    return str(config["feishu"].get("expected_app_id") or "").strip()


def _sync_cli_profile(
    current: dict[str, Any], *, tolerant: bool
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Pin cli_profile to the profile lark-cli actually resolves for the App ID.

    Agent bindings must resolve exactly (a resolution failure is an error);
    existing/dedicated bindings self-heal when the profile is discoverable and
    stay silent when it is not initialized yet.
    """
    app_id = _expected_app_id(current)
    if not app_id:
        return current, None
    try:
        resolution = resolve_lark_profile(app_id)
    except LarkCLIError:
        if not tolerant:
            raise
        return current, None
    if current["feishu"].get("cli_profile") == resolution["profile"]:
        return current, resolution

    return transition("feishu_profile", resolution["profile"])["config"], resolution


def _detect_agent_source() -> str:
    """Compatibility adapter for the setup journey's source detector."""
    return detect_agent_source()


def feishu_destination(destination: str) -> tuple[dict[str, Any], str]:
    return choose_destination(destination)


def import_feishu_host_context(
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

    return bind_agent_context(source, app_id, sender_open_id)


def feishu_context(*, verify: bool) -> tuple[dict[str, Any], str]:
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
        current, profile_resolution = _sync_cli_profile(current, tolerant=False)
    else:
        # Existing/dedicated bindings can also drift from lark-cli's real profile
        # name (e.g. a profile created externally as cli_<app_id>). Resolve by
        # App ID and self-heal when the profile is discoverable; never error when
        # the profile is simply not initialized yet.
        current, profile_resolution = _sync_cli_profile(current, tolerant=True)
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
    ready = _identity_ready(context, selected_identity)
    if not ready:
        if selected_identity == "user":
            if _authorization(current)["state"] == "waiting":
                return context, "resume_existing_user_base_authorization"
            return context, "run_feishu_auth_start"
        return context, "configure_bot_credentials_and_scopes_without_user_auth"
    if selected_identity == "user":
        return context, "reuse_existing_user_authorization_and_confirm_context"
    if not current["feishu"].get("manager_open_id"):
        return context, "resolve_and_save_feishu_manager"
    return context, "confirm_feishu_app_and_bot"


def feishu_identity(identity: str) -> dict[str, Any]:
    return choose_identity(identity)


def feishu_app(app_id: str) -> dict[str, Any]:
    result = choose_app(app_id)
    result["next_command"] = _secret_file_command()
    return result


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


def feishu_target(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Map an existing Base table by URL, verifying read access and fields."""
    base_token, table_id = _parse_feishu_base_url(arguments.url)
    config = load_config()
    identity = config["feishu"]["identity"]
    fields = list_fields(base_token, table_id, identity=identity)

    saved = transition(
        "feishu_target", {"base_token": base_token, "table_id": table_id}
    )["config"]
    return {
        "base_token": base_token,
        "table_id": table_id,
        "field_count": len(fields),
        "field_names": [str(f.get("name", "")) for f in fields],
        "enabled": saved["feishu"]["enabled"],
    }, "run_feishu_context_then_authorize_only_if_needed"


def feishu_setup() -> tuple[dict[str, Any], str]:
    config = load_config()
    secret_state = private_profile_secret_state()
    return setup_status(
        config,
        secret_state=secret_state,
        secret_command=_secret_file_command(),
        field_names=[spec["name"] for spec in standard_field_schema()],
        authorized_user=authorized_user_open_id,
    )


def _secret_file_path() -> Path:
    return data_dir() / SECRET_FILE_NAME


def _secret_file_command() -> str:
    return (
        "manage feishu-app-secret --prepare-secret-file → manage feishu-app-secret "
        "--open-secret-file（用户在打开的文件里粘贴并保存后）→ "
        "manage feishu-app-secret --secret-file <PATH>"
    )


def _secret_file_instructions(path: Path) -> list[str]:
    return [
        "打开文件后，把应用『凭证与基础信息』里的 App Secret 粘贴为一行，替换占位符整行。",
        "保存并关闭文件（多余的空行或文字会导致校验失败）。",
        "回到对话告诉 Agent 已完成，由它执行 consume 命令；文件读取后会被删除。",
    ]


def _prepare_secret_file() -> tuple[dict[str, Any], str]:
    """Create the restricted one-line secret file for the user to paste into."""
    path = _secret_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            SECRET_FILE_PLACEHOLDER + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        raise LarkCLIError(f"cannot prepare the App Secret file: {exc}") from exc
    return {
        "path": str(path),
        "created": True,
        "encrypted": False,
        "protection": "plaintext local file protected by the current OS user account permissions; consumed and deleted after one read",
        "contents_echoed": False,
        "instructions": _secret_file_instructions(path),
        "consume_command": f"manage feishu-app-secret --secret-file {path}",
    }, "edit_then_consume_feishu_secret_file"


def _open_secret_file() -> tuple[dict[str, Any], str]:
    """Open the prepared secret file in the user's default editor."""
    path = _secret_file_path()
    if not path.is_file():
        raise ConfigError(
            f"the App Secret file does not exist at {path}; prepare it first"
        )
    try:
        open_with_default_app(path)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LarkCLIError(f"cannot open the App Secret file editor: {exc}") from exc
    return {
        "path": str(path),
        "opened": True,
        "contents_echoed": False,
        "instructions": _secret_file_instructions(path),
        "consume_command": f"manage feishu-app-secret --secret-file {path}",
    }, "edit_then_consume_feishu_secret_file"


def _read_secret_file(value: Path) -> str:
    """Consume the prepared secret file: scoped, single-line, deleted on success."""
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ConfigError("the App Secret file cannot be a symbolic link")
    resolved = candidate.resolve()
    if resolved.parent != data_dir().resolve() or resolved.name != SECRET_FILE_NAME:
        raise ConfigError(
            "the App Secret file must be the prepared file inside the application "
            f"state directory ({data_dir() / SECRET_FILE_NAME})"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            raw = handle.read(SECRET_FILE_MAX_BYTES + 1)
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read the App Secret file: {exc}") from exc
    if len(raw.encode("utf-8")) > SECRET_FILE_MAX_BYTES:
        # Leave oversized files in place for the user to inspect manually.
        raise ConfigError("the App Secret file exceeds 64 KiB")
    secret = raw.strip()
    if not secret:
        raise ConfigError("the App Secret file is empty; paste the secret first")
    if secret == SECRET_FILE_PLACEHOLDER:
        raise ConfigError(
            "the App Secret file still contains the placeholder; paste the real "
            "secret and save before consuming"
        )
    if "\n" in secret or "\r" in secret:
        raise ConfigError(
            "the App Secret file must contain only the secret on a single line"
        )
    try:
        resolved.unlink()
    except OSError as exc:
        raise ConfigError(f"cannot remove the consumed App Secret file: {exc}") from exc
    return secret


def _store_app_secret(app_id: str, secret: str) -> dict[str, Any]:
    try:
        run_lark(
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
    return probe_app_secret_resolution()


def feishu_app_secret(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Collect one App Secret: prepared local file (default) or stdin pipe."""
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
    if arguments.prepare_secret_file:
        return _prepare_secret_file()
    if arguments.open_secret_file:
        return _open_secret_file()
    if arguments.secret_file:
        secret = _read_secret_file(Path(arguments.secret_file))
    else:
        secret = _read_secret_stdin("the Feishu App Secret")
    if not secret:
        raise ValueError("the App Secret is empty")
    probe = _store_app_secret(app_id, secret)
    return {
        "app_id": app_id,
        "secret_accepted": probe["resolvable"],
        "probe": probe,
    }, (
        "run_feishu_context_then_authorize_only_if_needed"
        if probe["resolvable"]
        else "provide_app_secret_for_private_profile"
    )


def feishu_local_profile(
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


def feishu_grant_manager(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
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


def feishu_create_base(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
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
    config = transition(
        "feishu_provision_anchor",
        {
            "base_token": base_token,
            "table_id": table_id,
            "base_name": base_name,
            "table_name": table_name,
        },
    )["config"]
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

    config = transition(
        "feishu_provision_complete", {"field_mapping": check["mapping"]}
    )["config"]
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
    }, "none"


def authorized_user_open_id() -> str:
    """Read the authorized user's Open ID from the isolated lark-cli state."""
    payload = run_lark(["auth", "status", "--json"], retries=1)
    auth = payload.get("data", payload) if isinstance(payload, dict) else {}
    identities = auth.get("identities", {}) if isinstance(auth, dict) else {}
    user = identities.get("user", {}) if isinstance(identities, dict) else {}
    return str(user.get("openId") or "").strip()


def feishu_manager(open_id: str) -> dict[str, Any]:
    return set_manager(open_id)


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
    return save_authorization_state(state, started=started, completed=completed)


def feishu_auth(arguments: argparse.Namespace) -> tuple[dict[str, Any], str]:
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
