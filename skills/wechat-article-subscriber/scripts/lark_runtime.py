"""Resolve lark-cli inside an isolated, path-stable application runtime."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import lark_profile_store
from paths import config_path, data_dir, secure_write_json

IDENTITY_ENV_KEYS = {
    "LARKSUITE_CLI_APP_ID",
    "LARKSUITE_CLI_APP_SECRET",
    "LARKSUITE_CLI_USER_ACCESS_TOKEN",
    "LARKSUITE_CLI_TENANT_ACCESS_TOKEN",
}
MAX_LARK_CONFIG_BYTES = lark_profile_store.MAX_CONFIG_BYTES
MAX_LARK_PROFILES = lark_profile_store.MAX_PROFILES


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


_EMPTY_BINDING = {"app_id": "", "profile": "", "binding_mode": "", "agent_source": ""}


def _runtime_binding() -> dict[str, str]:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(_EMPTY_BINDING)
    feishu = raw.get("feishu") if isinstance(raw, dict) else None
    if not isinstance(feishu, dict):
        return dict(_EMPTY_BINDING)
    return {
        "app_id": str(feishu.get("expected_app_id") or "").strip(),
        "profile": str(feishu.get("cli_profile") or "").strip(),
        "binding_mode": str(feishu.get("binding_mode") or "").strip(),
        "agent_source": str(feishu.get("agent_source") or "").strip(),
    }


def profile_name_for_app(app_id: str) -> str:
    """Compatibility adapter for the profile migration module."""
    return lark_profile_store.profile_name_for_app(app_id)


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
        if len(args) != 8 or any(
            args.count(flag) != 1 for flag in ("--source", "--app-id", "--identity")
        ):
            raise ValueError(
                "config bind accepts only the confirmed source, App ID, and "
                "user-default identity"
            )
        for flag, expected in (
            ("--source", binding["agent_source"]),
            ("--app-id", binding["app_id"]),
            ("--identity", "user-default"),
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


def safe_agent_lark_arguments(arguments: list[str]) -> list[str]:
    """Allow only the small non-mutating/auth-resume surface exposed to Agents."""
    args = list(arguments)
    allowed = args == ["--version"] or args == ["profile", "list"]
    allowed = allowed or (
        args[:2] == ["auth", "status"]
        and len(args) == len(set(args))
        and all(value in {"auth", "status", "--json", "--verify"} for value in args)
    )
    allowed = allowed or args == [
        "auth",
        "login",
        "--domain",
        "base",
        "--no-wait",
        "--json",
    ]
    allowed = allowed or args == ["auth", "login", "--help"]
    # The shared validator below binds this exact operation to the host source
    # and App ID already saved from trusted conversation context.
    allowed = allowed or args[:2] == ["config", "bind"]
    allowed = allowed or (
        len(args) in {4, 5}
        and args[:3] == ["auth", "login", "--device-code"]
        and bool(re.fullmatch(r"[A-Za-z0-9._~-]{1,256}", args[3]))
        and (len(args) == 4 or args[4] == "--json")
    )
    qr_url = args[2] if len(args) >= 3 else ""
    qr_path = Path(args[4]) if len(args) >= 5 and args[3] == "--output" else None
    qr_output = bool(
        len(args) in {5, 7}
        and args[:2] == ["auth", "qrcode"]
        and re.fullmatch(r"https://\S{1,2048}", qr_url)
        and qr_path is not None
        and not qr_path.is_absolute()
        and ".." not in qr_path.parts
        and not str(qr_path).startswith("-")
        and (
            len(args) == 5
            or (
                args[5] == "--size"
                and args[6].isdigit()
                and 128 <= int(args[6]) <= 2048
            )
        )
    )
    qr_ascii = bool(
        args[:2] == ["auth", "qrcode"]
        and len(args) == 4
        and re.fullmatch(r"https://\S{1,2048}", qr_url)
        and args[3] == "--ascii"
    )
    allowed = allowed or qr_output or qr_ascii or args == ["auth", "qrcode", "--help"]
    if not allowed:
        raise ValueError(
            "this lark command is outside the Agent-facing allowlist; use a "
            "purpose-built manage/process command so authorization is enforced"
        )
    return safe_lark_arguments(args)


def global_lark_config_path() -> Path:
    return lark_profile_store.config_path()


def global_lark_config_fingerprint() -> tuple[bool, int, int, str]:
    return lark_profile_store.fingerprint(global_lark_config_path())


def _read_lark_config(path: Path) -> dict[str, Any]:
    return lark_profile_store.read_config(path)


def _secret_storage(profile: dict[str, Any]) -> str:
    return lark_profile_store.secret_storage(profile)


def _metadata_text(value: Any, limit: int = 128) -> str:
    return lark_profile_store.metadata_text(value, limit)


def discover_global_lark_profiles() -> dict[str, Any]:
    """Return redacted metadata for the user's existing lark-cli profiles.

    This function never invokes lark-cli and never returns an App Secret, keychain
    identifier, access token, or user Open ID.
    """
    path = global_lark_config_path()
    return lark_profile_store.discover(
        path,
        fingerprint_fn=global_lark_config_fingerprint,
    )


def private_profile_secret_state() -> dict[str, Any]:
    """Local-only readiness check for the bound isolated profile's App Secret.

    Never invokes lark-cli (no device-auth probe, no network) and never returns
    a secret value. The setup dialogue uses this to ask for the App Secret
    before manager/target steps can suggest commands that would dead-end on a
    profile without credentials.
    """
    return lark_profile_store.private_secret_state(
        _runtime_binding(),
        lark_cli_config_dir(),
    )


def import_global_lark_profile(expected_app_id: str, target_profile: str) -> dict[str, Any]:
    """Clone one app credential into isolated state without modifying the source.

    User authorization entries are intentionally excluded because token refreshes
    can mutate shared keychain state. User identity must authorize once inside the
    isolated profile; bot identity can immediately reuse the copied App credential.
    """
    return lark_profile_store.import_profile(
        expected_app_id,
        target_profile,
        source_path=global_lark_config_path(),
        private_dir=lark_cli_config_dir(),
        fingerprint_fn=global_lark_config_fingerprint,
        secure_write=secure_write_json,
    )


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


class LarkCLIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "command",
        code: int | str | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.retryable = retryable


TESTED_LARK_CLI_VERSION = "1.0.69"
logger = logging.getLogger(__name__)


MIN_LARK_CLI_VERSION = (1, 0, 69)  # tested through 1.0.92 on 2026-08-30
MAX_LARK_CLI_MAJOR = 1


def _lark_cli() -> str:
    try:
        return str(resolve_lark_cli())
    except FileNotFoundError as exc:
        raise LarkCLIError(
            "lark-cli is not installed. Prerequisite: Node.js 18+ (nodejs.org or "
            "`brew install node`). Then install into the Skill's isolated directory: "
            "`npm install --prefix <doctor paths.data_dir>/lark-cli @larksuite/cli` "
            "(if npm ignores install scripts, also run `npm approve-scripts "
            "@larksuite/cli` in that directory so the native binary is downloaded). "
            "After installing, rerun `manage doctor` to confirm detection.",
            kind="missing_cli",
        ) from exc


def _execute_lark(
    args: list[str],
    *,
    input_text: str | None = None,
    capture_output: bool = True,
    timeout: int = 60,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Execute every lark-cli policy through the same isolated process boundary."""
    lark_cli_home_dir().mkdir(parents=True, exist_ok=True)
    lark_cli_config_dir().mkdir(parents=True, exist_ok=True)
    work_dir = lark_cli_work_dir()
    work_dir.mkdir(parents=True, exist_ok=True)
    global_before = global_lark_config_fingerprint()
    result = subprocess.run(
        [_lark_cli(), *args],
        input=input_text,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=lark_cli_environment(),
        cwd=work_dir,
    )
    return result, global_lark_config_fingerprint() == global_before


def lark_cli_info() -> dict[str, Any]:
    """Return a redacted compatibility report for the installed lark-cli."""
    try:
        result, global_unchanged = _execute_lark(["--version"], timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LarkCLIError(
            f"cannot run lark-cli version check: {type(exc).__name__}",
            kind="version",
        ) from exc
    if not global_unchanged:
        raise LarkCLIError(
            "the user's global ~/.lark-cli/config.json changed during an isolated "
            "Skill command; stop and inspect the CLI installation",
            kind="config",
        )
    executable = _lark_cli()
    output = (result.stdout or result.stderr).strip()[:200]
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", output)
    if result.returncode != 0 or not match:
        raise LarkCLIError(
            "cannot determine lark-cli version; reinstall the tested release",
            kind="version",
        )
    version_tuple = tuple(int(part) for part in match.groups())
    compatible = (
        version_tuple >= MIN_LARK_CLI_VERSION
        and version_tuple[0] <= MAX_LARK_CLI_MAJOR
    )
    try:
        profile_secret = private_profile_secret_state()
    except Exception:
        # Readiness must never break version reporting; the secret question
        # falls back to the network-backed probe paths.
        profile_secret = {"bound": False, "profile": "", "ready": False}
    return {
        "path": executable,
        "config_dir": str(lark_cli_config_dir()),
        "native_binary": executable.casefold().endswith(".exe") if os.name == "nt" else True,
        "global_config_protected": True,
        "version": ".".join(match.groups()),
        "tested_version": TESTED_LARK_CLI_VERSION,
        "compatible": compatible,
        "profile_secret": profile_secret,
    }


def _redact_cli_error(text: str, args: list[str]) -> str:
    redacted = text
    for flag in (
        "--base-token",
        "--table-id",
        "--record-id",
        "--device-code",
        "--token",
        "--member-id",
    ):
        positions = [index for index, value in enumerate(args) if value == flag]
        for index in positions:
            if index + 1 < len(args) and args[index + 1]:
                redacted = redacted.replace(args[index + 1], "<redacted>")
    return redacted[:1200]


def _json_value(text: str) -> dict[str, Any] | list[Any] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, (dict, list)) else None


def _payload_error(payload: dict[str, Any], args: list[str]) -> LarkCLIError:
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        error_type = str(error.get("type", ""))
        subtype = str(error.get("subtype", ""))
        message = _append_secret_hint(
            str(error.get("message") or error.get("msg") or "lark-cli request failed")
        )
        hint = str(error.get("hint") or "").strip()
        console_url = str(error.get("console_url") or "").strip()
        permission_violations = error.get("permission_violations")
    else:
        code = payload.get("code")
        error_type = ""
        message = str(error or payload.get("msg") or "lark-cli request failed")
        hint = str(payload.get("hint") or "").strip()
        console_url = str(payload.get("console_url") or "").strip()
        subtype = str(payload.get("subtype", ""))
        permission_violations = payload.get("permission_violations")
    code_text = f"[code {code}] " if str(code) not in ("", "None") else ""
    message = code_text + message
    violations_text = (
        json.dumps(permission_violations, ensure_ascii=False)
        if permission_violations
        else ""
    )
    informative_parts = [
        part for part in (subtype, hint, violations_text, console_url) if part.strip()
    ]
    if not informative_parts and message.replace(code_text, "").strip() in (
        "",
        "lark-cli request failed",
    ):
        # Some failures (e.g. a rejected App Secret during config init) return a
        # JSON payload without message/msg fields; include a redacted snippet so
        # the agent can see the underlying cause instead of a bare generic text.
        snippet = _redact_cli_error(
            json.dumps(payload, ensure_ascii=False)[:400], args
        ).strip()
        message = f"{code_text}lark-cli request failed | raw response: {snippet}"
    combined = " ".join(
        part for part in (message, subtype, hint, violations_text, console_url) if part
    )
    lower = combined.casefold()
    if error_type == "confirmation_required" or (
        error_type == "confirmation" and subtype == "confirmation_required"
    ):
        return LarkCLIError(
            "Feishu operation requires explicit confirmation; show the risk and wait for "
            "the user before retrying with --yes.",
            kind="confirmation_required",
            code=code,
        )
    if str(code) == "91403" or "don't have permission" in lower or "no permission" in lower:
        return LarkCLIError(
            "Feishu Base is not writable by the current user (91403). Verify the Base "
            "share/role permission; do not retry or silently switch to bot.",
            kind="permission",
            code=code,
        )
    if "not configured" in lower or error_type == "not_configured":
        # lark-cli reports a bare "not configured" when the pinned profile has
        # no usable App credential; name the Skill-level fix explicitly so the
        # dialogue can recover instead of dead-ending.
        return LarkCLIError(
            "the isolated lark-cli profile has no usable credentials for the bound "
            "App ID; run `manage feishu-app-secret --prepare-secret-file`, then "
            "`--open-secret-file`, enter the secret locally and consume it with "
            "`manage feishu-app-secret --secret-file <PATH>` (bot identity needs no OAuth) and retry",
            kind="config",
            code=code,
        )
    if "client_secret" in lower or "config init --new" in lower:
        # A device-authorization request missing client_secret means the
        # isolated profile cannot decrypt the keychain-stored App Secret; this
        # is a configuration gap, not a user-authorization problem, so it must
        # be classified before the generic authorization bucket below.
        return LarkCLIError(
            _redact_cli_error(message, args),
            kind="config",
        )
    if str(code) in {"99991672", "99991679"} or any(
        marker in lower
        for marker in (
            "need_user_authorization",
            "authorization",
            "access token",
            "permission_violations",
            "missing scope",
        )
    ):
        guidance = (
            "Feishu authorization or app scope is missing (bot identity: check the "
            "App Secret via manage feishu-app-secret and the app's Base permissions "
            "in the console - do not run user auth). Request only the base domain"
        )
        if console_url:
            guidance += f" and open the developer-console link: {console_url}"
        elif hint:
            guidance += f". {hint}"
        return LarkCLIError(
            _redact_cli_error(guidance, args), kind="authorization", code=code
        )
    if (
        error_type
        in {
            "member_already_exists",
            "member_exist",
            "already_exists",
            "duplicate",
        }
        or any(
            marker in lower
            for marker in (
                "already exists",
                "already a member",
                "already member",
                "duplicate member",
                "member already",
                "has been added",
            )
        )
    ):
        # Re-granting a resource to the same manager is idempotent: lark-cli
        # reports the member as already present. Resume flows may treat this as
        # success; every other failure must keep failing loudly.
        return LarkCLIError(
            "the Feishu resource is already shared with this manager; treat as granted",
            kind="duplicate",
            code=code,
        )
    retryable = (
        str(code) in {"429", "1254291"}
        or error_type in {"network", "timeout", "rate_limit"}
        or any(
            marker in lower
            for marker in ("timeout", "temporarily", "connection reset", "rate limit", "try again")
        )
    )
    return LarkCLIError(
        _redact_cli_error(combined or "lark-cli request failed", args),
        kind="transient" if retryable else "api",
        code=code,
        retryable=retryable,
    )


def run_lark(
    args: list[str], *, retries: int = 3, input_text: str | None = None
) -> dict[str, Any] | list[Any]:
    # input_text is forwarded to the child's stdin; used exclusively for
    # secrets, which must never reach argv, logs, or error text.
    try:
        safe_args = safe_lark_arguments(args)
    except ValueError as exc:
        raise LarkCLIError(str(exc), kind="config") from exc
    last_error: LarkCLIError | None = None
    for attempt in range(max(1, retries)):
        try:
            result, global_unchanged = _execute_lark(safe_args, input_text=input_text)
            if not global_unchanged:
                raise LarkCLIError(
                    "the user's global ~/.lark-cli/config.json changed during an "
                    "isolated Skill command; stop and inspect the CLI installation",
                    kind="config",
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = LarkCLIError(
                f"lark-cli transient process failure: {type(exc).__name__}",
                kind="transient",
                retryable=True,
            )
        else:
            payload = _json_value(result.stdout)
            if payload is None:
                payload = _json_value(result.stderr)
            if result.returncode == 0 and payload is not None:
                if isinstance(payload, dict) and (
                    payload.get("ok") is False
                    or payload.get("code") not in (None, 0)
                ):
                    last_error = _payload_error(payload, args)
                else:
                    return payload
            elif isinstance(payload, dict):
                last_error = _payload_error(payload, args)
            else:
                output = result.stderr.strip() or result.stdout.strip()
                last_error = LarkCLIError(
                    _redact_cli_error(
                        f"lark-cli exited {result.returncode} with non-JSON output: {output}",
                        args,
                    ),
                    kind="command",
                )
        if last_error is None or not last_error.retryable or attempt >= retries - 1:
            break
        time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def run_agent_lark(arguments: list[str]) -> tuple[int, bool, str, str]:
    """Run the restricted Agent-facing CLI with isolated, redacted output."""
    safe_args = safe_agent_lark_arguments(arguments)
    result, global_unchanged = _execute_lark(safe_args)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode:
        stdout = _redact_cli_error(stdout, safe_args)
        stderr = _redact_cli_error(stderr, safe_args)
    return result.returncode, global_unchanged, stdout, stderr


def _append_secret_hint(message: str) -> str:
    """Explain the isolated-profile keychain limitation when the CLI cannot."""
    lowered = message.casefold()
    if "client_secret" not in lowered and "config init --new" not in lowered:
        return message
    return (
        message
        + " | the isolated Skill profile references an App Secret stored in the "
        "global lark-cli keychain, which cannot be decrypted from the isolated "
        "configuration directory. Copy the App Secret from the Feishu Open "
        "Platform console (open.feishu.cn) into the prepared local secret file: "
        "`manage feishu-app-secret --prepare-secret-file`, `--open-secret-file`, "
        "then `--secret-file <PATH>` (do not run `config init --new`)."
    )
