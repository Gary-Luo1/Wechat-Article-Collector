#!/usr/bin/env python3
"""Validate and persist configuration from Agent stdin or a local wizard."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from config_store import (
    DEFAULT_CONFIG,
    ConfigError,
    load_config,
    save_config,
    validate_config,
)
from config_transitions import (
    apply_agent_payload,
    apply_section_patch,
    persist,
)
from paths import config_path, data_dir, secure_write_json
from protocol import dump, emit, failure, success

MAX_AGENT_INPUT_BYTES = 256 * 1024


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
    emit(
        success(result, next_action="edit_then_validate_local_config"),
        json_output=json_output,
        text=f"Opened local configuration: {target}",
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
                "Use a process stdin API with setup --agent-stdin; never interpolate a "
                "secret into shell text. Prefer local self-editing or hidden prompts "
                "when the Agent lacks a separate stdin channel."
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
                    "local self-editing or the hidden prompts in setup"
                ),
                "subscriptions": (
                    "account entries; each needs the WeChat alias (微信号) — the data source "
                    "queries by alias only; the bundled assets/default_subscriptions.json "
                    "roster (name + alias, no paid resolution) can seed this list"
                ),
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
                "Edit the prepared local configuration file or run setup locally for hidden input.",
                "Prefer local entry; chat requires explicit retention consent and must be permitted by host rules. Never put secrets in command text.",
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
            "ask_protocol": (
                "Apply values already supplied, then re-run the wizard and ask only for "
                "missing decisions. Related non-secret questions may be grouped; "
                "keep credential-channel consent and authorization explicit."
            ),
            "collect_before_execution": [
                "credential input channel",
                "redfox API key via local self-editing, hidden prompt, or a supported safe transport",
                "subscriptions (preview/apply the bundled default roster first, then user adjustments)",
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


def config_from_agent_payload(
    payload: Any, *, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    return apply_agent_payload(payload, existing=existing)


def _apply_section_patch(
    config: dict[str, Any], section: str, payload: Any
) -> dict[str, Any]:
    return apply_section_patch(config, section, payload)


def _save_agent_raw(raw: str, *, section: str = "full", json_output: bool = False) -> int:
    if len(raw.encode("utf-8")) > MAX_AGENT_INPUT_BYTES:
        logging.error("Agent configuration exceeds the input size limit")
        return 1
    try:
        payload = json.loads(raw.lstrip("\ufeff"))
        if section == "full":
            config = persist("agent_payload", payload)
        else:
            config = persist(f"section:{section}", payload)
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
    emit(success({"inbox_path": path}), json_output=json_output, text=path)
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
