"""Feishu Base adapter with explicit identity and schema mapping."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from lark_runtime import (
    global_lark_config_fingerprint,
    lark_cli_config_dir,
    lark_cli_environment,
    lark_cli_home_dir,
    lark_cli_work_dir,
    resolve_lark_cli,
    safe_lark_arguments,
)
from url_identity import upgrade_wechat_article_url


FIELD_SPECS: dict[str, dict[str, Any]] = {
    "title": {
        "name": "文章标题",
        "type": "text",
        "numeric_type": 1,
        "aliases": ["文章标题", "标题", "title", "文章"],
        "required": True,
    },
    "account": {
        "name": "公众号名称",
        "type": "text",
        "numeric_type": 1,
        "aliases": ["公众号名称", "公众号", "账号名称", "来源", "account"],
    },
    "account_id": {
        "name": "公众号ID",
        "type": "text",
        "numeric_type": 1,
        "aliases": ["公众号ID", "公众号id", "微信号", "账号ID", "account id"],
    },
    "url": {
        "name": "文章链接",
        "type": "text",
        "numeric_type": 15,
        "style": {"type": "url"},
        "aliases": ["文章链接", "链接", "文章URL", "URL", "url", "原文链接"],
        "required": True,
        "accepted_types": {"text", "url"},
    },
    "summary": {
        "name": "文章摘要",
        "type": "text",
        "numeric_type": 1,
        "aliases": ["文章摘要", "摘要", "总结", "summary"],
    },
    "published_at": {
        "name": "发布日期",
        "type": "datetime",
        "numeric_type": 5,
        "style": {"format": "yyyy-MM-dd HH:mm"},
        "aliases": ["发布日期", "发布时间", "发布于", "publish time"],
    },
    "fetched_at": {
        "name": "抓取时间",
        "type": "datetime",
        "numeric_type": 5,
        "style": {"format": "yyyy-MM-dd HH:mm"},
        "aliases": ["抓取时间", "采集时间", "同步时间", "fetch time"],
    },
    "score": {
        "name": "AI评分",
        "type": "number",
        "numeric_type": 2,
        "style": {"type": "plain", "precision": 1},
        "aliases": ["AI评分", "评分", "得分", "score"],
    },
    "rationale": {
        "name": "评分理由",
        "type": "text",
        "numeric_type": 1,
        "aliases": ["评分理由", "评分依据", "理由", "rationale"],
    },
    "tags": {
        "name": "文章标签",
        "type": "text",
        "numeric_type": 1,
        "aliases": ["文章标签", "标签", "tags"],
        "accepted_types": {"text", "select"},
    },
    "read_status": {
        "name": "阅读状态",
        "type": "select",
        "numeric_type": 3,
        "multiple": False,
        "options": [{"name": "未读"}, {"name": "已读"}, {"name": "忽略"}],
        "aliases": ["阅读状态", "状态", "read status"],
    },
}

READ_ONLY_TYPES = {
    "auto_number",
    "lookup",
    "formula",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
    "attachment",
    "not_support",
}
NUMERIC_TYPES = {
    1: "text",
    2: "number",
    3: "select",
    5: "datetime",
    15: "url",
    17: "attachment",
    18: "created_at",
    19: "updated_at",
    20: "formula",
    21: "link",
    1001: "created_by",
    1002: "updated_by",
}


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


def standard_field_schema() -> list[dict[str, Any]]:
    """Return the current field JSON for a newly authorized Base/table."""
    fields: list[dict[str, Any]] = []
    for spec in FIELD_SPECS.values():
        fields.append(
            {
                key: value
                for key, value in spec.items()
                if key
                not in {"numeric_type", "aliases", "required", "accepted_types"}
            }
        )
    return fields


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


def lark_cli_info() -> dict[str, Any]:
    """Return a redacted compatibility report for the installed lark-cli."""
    executable = _lark_cli()
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
            env=lark_cli_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LarkCLIError(
            f"cannot run lark-cli version check: {type(exc).__name__}",
            kind="version",
        ) from exc
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
    return {
        "path": executable,
        "config_dir": str(lark_cli_config_dir()),
        "native_binary": executable.casefold().endswith(".exe") if os.name == "nt" else True,
        "global_config_protected": True,
        "version": ".".join(match.groups()),
        "tested_version": TESTED_LARK_CLI_VERSION,
        "compatible": compatible,
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


def _run_lark(
    args: list[str], *, retries: int = 3, input_text: str | None = None
) -> dict[str, Any] | list[Any]:
    # input_text is forwarded to the child's stdin; used exclusively for
    # secrets, which must never reach argv, logs, or error text.
    try:
        safe_args = safe_lark_arguments(args)
    except ValueError as exc:
        raise LarkCLIError(str(exc), kind="config") from exc
    command = [_lark_cli(), *safe_args]
    lark_cli_home_dir().mkdir(parents=True, exist_ok=True)
    lark_cli_config_dir().mkdir(parents=True, exist_ok=True)
    work_dir = lark_cli_work_dir()
    work_dir.mkdir(parents=True, exist_ok=True)
    global_before = global_lark_config_fingerprint()
    last_error: LarkCLIError | None = None
    for attempt in range(max(1, retries)):
        try:
            result = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
                env=lark_cli_environment(),
                cwd=work_dir,
            )
            if global_lark_config_fingerprint() != global_before:
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
        "Platform console (open.feishu.cn) and run the supported stdin init: "
        "`printf %s '<APP_SECRET>' | lark config init --app-id <APP_ID> "
        "--app-secret-stdin` (do not run `config init --new`)."
    )


def probe_app_secret_resolution() -> dict[str, Any]:
    """Check the isolated profile can actually decrypt its App Secret.

    Starts one device-authorization request (no user action, no state change;
    the pending code simply expires) because keychain-backed secrets only
    surface as masked values in `config show` — resolution can only be proven
    by a call that uses the secret.
    """
    try:
        _run_lark(["auth", "login", "--domain", "base", "--no-wait", "--json"], retries=1)
    except LarkCLIError as exc:
        message = str(exc)
        if "client_secret" in message:
            return {
                "resolvable": False,
                "reason": "keychain_secret_not_migratable",
                "remediation": (
                    "copy the App Secret from the Feishu Open Platform console "
                    "(open.feishu.cn) for the bound App ID, then pipe it into "
                    "manage feishu-app-secret (bash: printf %s '<APP_SECRET>' | "
                    "manage feishu-app-secret; PowerShell: '<APP_SECRET>' | manage "
                    "feishu-app-secret)"
                ),
            }
        return {"resolvable": False, "reason": "api_error", "message": message[:200]}
    return {"resolvable": True}


def _items(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "records", "tables"):
        value = data.get(key)
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return [item for item in value if isinstance(item, dict)]
    value = data.get("fields")
    if isinstance(value, list) and any(isinstance(item, dict) for item in value):
        return [item for item in value if isinstance(item, dict)]
    # lark-cli >= 1.0.9x table mode: rows live in data.data with parallel
    # record_id_list (no per-record objects). Rebuild dict rows so callers
    # keep a uniform shape.
    rows = data.get("data")
    record_ids = data.get("record_id_list")
    field_names = data.get("fields")
    if isinstance(rows, list) and isinstance(record_ids, list):
        if len(rows) > len(record_ids):
            logger.warning(
                "record payload truncated: %d rows but %d record ids",
                len(rows),
                len(record_ids),
            )
        return [
            {
                "record_id": record_ids[index],
                "fields": dict(zip(field_names or [], row)) if isinstance(row, list) else row,
            }
            for index, row in enumerate(rows)
            if index < len(record_ids)
        ]
    return []


def list_fields(
    base_token: str, table_id: str, *, identity: str = "user"
) -> list[dict[str, Any]]:
    return _items(
        _run_lark(
            [
                "base",
                "+field-list",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--as",
                identity,
                "--limit",
                "200",
                "--format",
                "json",
            ]
        )
    )


def _field_name(field: dict[str, Any]) -> str:
    return str(field.get("field_name") or field.get("name") or "").strip()


def _field_id(field: dict[str, Any]) -> str:
    return str(field.get("field_id") or field.get("id") or "").strip()


def _field_type(field: dict[str, Any]) -> str:
    raw = field.get("type", field.get("field_type"))
    if isinstance(raw, int):
        return NUMERIC_TYPES.get(raw, str(raw))
    normalized = re.sub(r"[^a-z0-9]", "", str(raw).casefold())
    aliases = {
        "singleselect": "select",
        "multiselect": "select",
        "date": "datetime",
        "numeric": "number",
        "hyperlink": "url",
        "createdtime": "created_at",
        "modifiedtime": "updated_at",
        "autonumber": "auto_number",
    }
    return aliases.get(normalized, normalized)


def _normalized_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _accepted_types(spec: dict[str, Any]) -> set[str]:
    return set(spec.get("accepted_types", {spec["type"]}))


def _validate_field(logical: str, field: dict[str, Any]) -> None:
    actual = _field_type(field)
    if actual in READ_ONLY_TYPES:
        raise LarkCLIError(
            f"mapped Feishu field {_field_name(field)!r} is read-only ({actual})",
            kind="field_mapping",
        )
    if actual not in _accepted_types(FIELD_SPECS[logical]):
        raise LarkCLIError(
            f"mapped Feishu field {_field_name(field)!r} has type {actual!r}; "
            f"expected one of {sorted(_accepted_types(FIELD_SPECS[logical]))}",
            kind="field_mapping",
        )


def resolve_field_mapping(
    fields: list[dict[str, Any]], configured: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Resolve configured/known aliases to actual writable field IDs."""
    configured = configured or {}
    by_id = {_field_id(field): field for field in fields if _field_id(field)}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        name = _field_name(field)
        if name:
            by_name.setdefault(_normalized_name(name), []).append(field)
    resolved: dict[str, dict[str, Any]] = {}
    used_targets: dict[str, str] = {}
    for logical, spec in FIELD_SPECS.items():
        target = configured.get(logical)
        candidates: list[dict[str, Any]] = []
        if isinstance(target, dict):
            target_id = str(target.get("field_id", "")).strip()
            target_name = str(target.get("name", "")).strip()
            if target_id and target_id in by_id:
                candidates = [by_id[target_id]]
            elif target_name:
                candidates = by_name.get(_normalized_name(target_name), [])
            if not candidates:
                raise LarkCLIError(
                    f"configured Feishu mapping for {logical!r} no longer exists; "
                    "inspect the table and confirm a new mapping",
                    kind="field_mapping",
                )
        else:
            seen: set[str] = set()
            for alias in spec["aliases"]:
                for candidate in by_name.get(_normalized_name(alias), []):
                    candidate_type = _field_type(candidate)
                    if (
                        candidate_type in READ_ONLY_TYPES
                        or candidate_type not in _accepted_types(spec)
                    ):
                        continue
                    identity = _field_id(candidate) or _field_name(candidate)
                    if identity not in seen:
                        candidates.append(candidate)
                        seen.add(identity)
        if len(candidates) > 1:
            raise LarkCLIError(
                f"Feishu field mapping for {logical!r} is ambiguous; ask the user to choose",
                kind="field_mapping",
            )
        if not candidates:
            if spec.get("required"):
                raise LarkCLIError(
                    f"Feishu table has no compatible {logical!r} field. Map an existing "
                    "field or obtain confirmation before extending the schema.",
                    kind="field_mapping",
                )
            continue
        field = candidates[0]
        _validate_field(logical, field)
        field_identity = _field_id(field) or _normalized_name(_field_name(field))
        if field_identity in used_targets:
            raise LarkCLIError(
                f"Feishu field {_field_name(field)!r} is mapped to both "
                f"{used_targets[field_identity]!r} and {logical!r}; choose distinct fields",
                kind="field_mapping",
            )
        used_targets[field_identity] = logical
        resolved[logical] = {
            "field_id": _field_id(field),
            "name": _field_name(field),
            "type": _field_type(field),
            "raw": field,
        }
    return resolved


def _datetime_value(timestamp: Any) -> str:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        value = time.time()
    return datetime.fromtimestamp(value, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _https_wechat_url(value: Any) -> str:
    return upgrade_wechat_article_url(value)


def _required_score(metadata: dict[str, Any]) -> float:
    if "score" not in metadata:
        raise ArticleNotSyncableError(
            "this processed entry has no score/summary to build a Feishu record "
            "(dismissed or legacy entry); restore and complete it properly first"
        )
    return float(metadata["score"])


class ArticleNotSyncableError(ValueError):
    """A processed entry lacks the data a Feishu record needs."""

    code = "ARTICLE_NOT_SYNCABLE"
    retryable = False


def _logical_record(article: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    tags = metadata.get("tags", [])
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    return {
        "title": str(article.get("title", "")),
        "account": str(article.get("account", "")),
        "account_id": str(article.get("account_id", article.get("account", ""))),
        "url": _https_wechat_url(article.get("link", "")),
        "summary": str(metadata.get("summary") or article.get("digest", ""))[:500],
        "published_at": _datetime_value(article.get("update_time")),
        "fetched_at": _datetime_value(time.time()),
        "score": _required_score(metadata),
        "rationale": str(metadata.get("rationale", ""))[:2000],
        "tags": [str(tag) for tag in tags],
        "read_status": "未读",
    }


def _select_options(field: dict[str, Any]) -> set[str]:
    candidates = [field.get("options")]
    property_value = field.get("property")
    if isinstance(property_value, dict):
        candidates.append(property_value.get("options"))
    for value in candidates:
        if isinstance(value, list):
            return {
                str(item.get("name", ""))
                for item in value
                if isinstance(item, dict) and item.get("name")
            }
    return set()


def _field_multiple(field: dict[str, Any]) -> bool:
    if field.get("multiple") is not None:
        return bool(field.get("multiple"))
    property_value = field.get("property")
    return bool(property_value.get("multiple")) if isinstance(property_value, dict) else False


def build_mapped_record(
    article: dict[str, Any],
    metadata: dict[str, Any],
    mapping: dict[str, dict[str, Any]],
) -> "tuple[dict[str, Any], list[str]]":
    """Return (record, skipped_fields) so callers can surface silent drops."""
    logical = _logical_record(article, metadata)
    record: dict[str, Any] = {}
    skipped: list[str] = []
    for key, target in mapping.items():
        value = logical[key]
        actual_type = target["type"]
        raw = target["raw"]
        if key == "tags":
            if actual_type == "select":
                options = _select_options(raw)
                if not options or not set(value).issubset(options):
                    skipped.append(target["name"])
                    continue
                multiple = _field_multiple(raw)
                value = value if multiple else (value[0] if value else None)
            else:
                value = ", ".join(value)
        elif actual_type == "select":
            options = _select_options(raw)
            if not options or str(value) not in options:
                skipped.append(target["name"])
                continue
        field_key = target["field_id"] or target["name"]
        record[field_key] = value
    return record, skipped


def find_record_by_url(
    base_token: str,
    table_id: str,
    url: str,
    url_field: str,
    *,
    identity: str = "user",
) -> str | None:
    filter_json = {"logic": "and", "conditions": [[url_field, "==", url]]}
    payload = _run_lark(
        [
            "base",
            "+record-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--filter-json",
            json.dumps(filter_json, ensure_ascii=False),
            "--limit",
            "2",
            "--as",
            identity,
            "--format",
            "json",
        ]
    )
    matches = _items(payload)
    if len(matches) > 1:
        raise LarkCLIError(
            "multiple Feishu records have the same article URL", kind="duplicate"
        )
    if not matches:
        return None
    return str(matches[0].get("record_id") or matches[0].get("id") or "") or None


def _find_values(value: Any, names: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold().replace("_", "") in names and isinstance(item, str):
                found.append(item)
            found.extend(_find_values(item, names))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_values(item, names))
    return found


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else {}


def _profile_items(payload: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    for candidate in (
        payload.get("data"),
        payload.get("profiles"),
        payload.get("items"),
    ):
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            for key in ("profiles", "items"):
                nested = candidate.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def resolve_lark_profile(expected_app_id: str) -> dict[str, Any]:
    """Resolve one lark-cli profile by the trusted current-conversation App ID."""
    expected = expected_app_id.strip()
    if not expected:
        raise LarkCLIError(
            "the current Feishu conversation App ID is missing; never fall back to "
            "the lark-cli default profile",
            kind="wrong_app",
        )
    profiles = _profile_items(_run_lark(["profile", "list"], retries=1))
    matches = [
        profile
        for profile in profiles
        if str(profile.get("appId") or profile.get("app_id") or "").strip()
        == expected
    ]
    if not matches:
        raise LarkCLIError(
            f"no lark-cli profile matches the current Feishu conversation App ID "
            f"{expected}. Bind or initialize that exact bot before continuing; the "
            "default profile was not used.",
            kind="wrong_app",
        )
    if len(matches) > 1:
        raise LarkCLIError(
            f"multiple lark-cli profiles match the current Feishu conversation App ID "
            f"{expected}. Resolve the duplicate profiles before continuing; the default "
            "profile was not used.",
            kind="wrong_app",
        )
    matched = matches[0]
    profile_name = str(matched.get("name") or matched.get("profile") or "").strip()
    if (
        not profile_name
        or len(profile_name) > 128
        or any(ord(character) < 32 for character in profile_name)
    ):
        raise LarkCLIError(
            "the lark-cli profile matching the current bot has an invalid name",
            kind="config",
        )
    active_profiles = [
        profile
        for profile in profiles
        if profile.get("active") is True
    ]
    active_app_ids = {
        str(profile.get("appId") or profile.get("app_id") or "").strip()
        for profile in active_profiles
    }
    return {
        "profile": profile_name,
        "app_id": expected,
        "matched_by": "current_conversation_app_id",
        "match_count": 1,
        "default_profile_ignored": bool(active_app_ids - {expected}),
        "secrets_included": False,
    }


def feishu_identity_context(*, verify: bool = False) -> dict[str, Any]:
    """Return a redacted, stable identity snapshot for dialogue setup."""
    configured_payload = _run_lark(["config", "show"], retries=1)
    if not isinstance(configured_payload, dict):
        raise LarkCLIError("lark-cli config show returned an invalid payload", kind="command")
    configured = _payload_data(configured_payload)
    auth_args = ["auth", "status", "--json"]
    if verify:
        auth_args.append("--verify")
    auth_payload = _run_lark(auth_args, retries=1)
    if not isinstance(auth_payload, dict):
        raise LarkCLIError("lark-cli auth status returned an invalid payload", kind="command")
    auth = _payload_data(auth_payload)
    app_ids = list(
        dict.fromkeys(
            value.strip()
            for value in [
                *_find_values(configured_payload, {"appid"}),
                *_find_values(auth_payload, {"appid"}),
            ]
            if value.strip()
        )
    )
    identities = auth.get("identities") if isinstance(auth.get("identities"), dict) else {}
    user = identities.get("user") if isinstance(identities.get("user"), dict) else {}
    bot = identities.get("bot") if isinstance(identities.get("bot"), dict) else {}
    return {
        "app_id": app_ids[0] if len(app_ids) == 1 else "",
        "app_ids": app_ids,
        "app_id_unambiguous": len(app_ids) == 1,
        "profile": str(configured.get("profile") or ""),
        "workspace": str(configured.get("workspace") or ""),
        "brand": str(configured.get("brand") or ""),
        "active_identity": str(auth.get("identity") or auth.get("activeIdentity") or ""),
        "user": {
            "available": bool(user.get("available")),
            "status": str(user.get("status") or ""),
            "token_status": str(user.get("tokenStatus") or ""),
            "name": str(user.get("userName") or ""),
            "open_id": str(user.get("openId") or ""),
        },
        "bot": {
            "available": bool(bot.get("available")),
            "status": str(bot.get("status") or ""),
        },
        "secrets_included": False,
    }


def verify_feishu_identity(
    feishu: dict[str, Any], *, identity: str | None = None
) -> dict[str, Any]:
    """Prove the active isolated profile matches the confirmed app and identity."""
    selected_identity = identity or str(feishu.get("identity") or "user")
    expected_app_id = str(feishu.get("expected_app_id") or "").strip()
    if not expected_app_id:
        raise LarkCLIError(
            "Feishu App ID has not been explicitly confirmed. Run manage "
            "feishu-context, show the detected App ID and user, then save the confirmed "
            "App ID before authorizing or writing.",
            kind="wrong_app",
        )
    if str(feishu.get("binding_mode") or "") == "agent":
        resolved = resolve_lark_profile(expected_app_id)
        configured_profile = str(feishu.get("cli_profile") or "").strip()
        if configured_profile != resolved["profile"]:
            raise LarkCLIError(
                "the locally pinned lark-cli profile no longer matches the current "
                "Feishu conversation App ID. Run manage feishu-context to resolve and "
                "pin the exact profile before continuing.",
                kind="wrong_app",
            )
    auth = _run_lark(["auth", "status", "--json", "--verify"], retries=1)
    if not isinstance(auth, dict):
        raise LarkCLIError("lark-cli auth status returned an invalid payload", kind="command")
    auth_data = auth.get("data", auth)
    identities = auth_data.get("identities", {}) if isinstance(auth_data, dict) else {}
    identity_status = (
        identities.get(selected_identity, {}) if isinstance(identities, dict) else {}
    )
    actual_ids = list(dict.fromkeys(_find_values(auth, {"appid"})))
    if len(actual_ids) != 1 or actual_ids[0] != expected_app_id:
        raise LarkCLIError(
            "lark-cli could not prove that the active profile uses the confirmed Feishu "
            "App ID. Re-select or initialize the profile, run manage feishu-context, "
            "and confirm by App ID rather than bot display name.",
            kind="wrong_app",
        )
    if (
        not isinstance(identity_status, dict)
        or not identity_status.get("available")
        or identity_status.get("status") != "ready"
        or (
            selected_identity == "user"
            and identity_status.get("tokenStatus")
            not in {None, "valid", "needs_refresh"}
        )
    ):
        label = "user authorization" if selected_identity == "user" else "bot identity"
        raise LarkCLIError(
            f"Feishu {label} is not ready. "
            + (
                "A needs_refresh token auto-refreshes on the next real API call, so "
                "retrying the sync usually succeeds; only start split-flow "
                "authorization (auth login --domain base --no-wait --json) if a "
                "retry still fails."
                if selected_identity == "user"
                else "Configure the app secret and required backend scopes; do not run user auth."
            ),
            kind="authorization",
        )
    expected_user_open_id = str(feishu.get("expected_user_open_id") or "").strip()
    if selected_identity == "user" and expected_user_open_id:
        actual_users = list(dict.fromkeys(_find_values(identity_status, {"openid"})))
        if not actual_users or expected_user_open_id not in actual_users:
            raise LarkCLIError(
                "the authorized Feishu user does not match the user confirmed during "
                "setup; run manage feishu-context and authorize the intended user",
                kind="wrong_app",
            )
    return {
        "identity": selected_identity,
        "app_id": expected_app_id,
        "status": "ready",
    }


RESOURCE_TYPES = {
    "bitable",
    "doc",
    "docx",
    "file",
    "folder",
    "sheet",
    "slides",
    "wiki",
}


def grant_bot_created_resource(
    token: str, resource_type: str, manager_open_id: str
) -> dict[str, Any]:
    """Grant full access to the human manager of a bot-created resource."""
    resource_token = token.strip()
    member = manager_open_id.strip()
    normalized_type = resource_type.strip().casefold()
    if not resource_token:
        raise ValueError("resource token is required")
    if normalized_type not in RESOURCE_TYPES:
        raise ValueError(
            f"unsupported Feishu resource type: {resource_type}; "
            f"choose one of {sorted(RESOURCE_TYPES)}"
        )
    if not member.startswith("ou_"):
        raise ValueError("manager_open_id must be a confirmed Feishu open_id starting with ou_")
    return _run_lark(
        [
            "drive",
            "+member-add",
            "--token",
            resource_token,
            "--type",
            normalized_type,
            "--member-id",
            member,
            "--member-type",
            "openid",
            "--perm",
            "full_access",
            "--as",
            "bot",
            "--yes",
            "--format",
            "json",
        ],
        retries=1,
    )


def create_standard_base(
    name: str,
    table_name: str,
    *,
    identity: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create the standard schema without exposing JSON to a user shell."""
    base_name = " ".join(name.split())
    first_table_name = " ".join(table_name.split())
    if not base_name or not first_table_name:
        raise ValueError("Base name and table name are required")
    if identity not in {"user", "bot"}:
        raise ValueError("identity must be user or bot")
    fields_json = json.dumps(
        standard_field_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # The standard schema is intentionally bounded. More fields should be added
    # in separate field-create calls, not by growing one Windows command line.
    if len(fields_json.encode("utf-8")) > 8 * 1024:
        raise ValueError("standard field schema is too large for safe Base creation")
    # lark-cli reads @file with a path relative to its working directory, and
    # inline JSON breaks under Windows cmd quoting. Write the bounded schema to
    # the CLI work directory and reference it by bare filename.
    fields_path = lark_cli_work_dir() / f"base-fields-{os.getpid()}.json"
    fields_path.write_text(fields_json, encoding="utf-8")
    arguments = [
        "base",
        "+base-create",
        "--name",
        base_name,
        "--table-name",
        first_table_name,
        "--fields",
        f"@{fields_path.name}",
        "--as",
        identity,
        "--format",
        "json",
    ]
    try:
        if dry_run:
            arguments.append("--dry-run")
        return _run_lark(arguments, retries=1)
    finally:
        fields_path.unlink(missing_ok=True)


def created_base_identifiers(payload: dict[str, Any]) -> tuple[str, str]:
    base_tokens = list(
        dict.fromkeys(
            value.strip()
            for value in _find_values(
                payload, {"basetoken", "apptoken", "createdbasetoken"}
            )
            if value.strip()
        )
    )
    # lark-cli >= 1.0.9x nests the created table under data.table as an object
    # ({"id": "tbl...", ...}); the generic string scan below cannot see it.
    nested_table = _payload_data(payload).get("table")
    nested_table_id = str(
        nested_table.get("id", "") if isinstance(nested_table, dict) else ""
    ).strip()
    table_ids = list(
        dict.fromkeys(
            [nested_table_id]
            + [
                value.strip()
                for value in _find_values(
                    payload, {"tableid", "defaulttableid", "createdtableid"}
                )
                if value.strip()
            ]
        )
    )
    base_token = next(
        (value for value in base_tokens if not value.startswith("<")), ""
    )
    table_id = next(
        (value for value in table_ids if value.startswith("tbl")), ""
    )
    if not base_token or not table_id:
        raise LarkCLIError(
            "the Base may have been created, but lark-cli did not return a usable "
            "base token and table ID; inspect the creation result before retrying",
            kind="api",
        )
    return base_token, table_id


def preflight_feishu(
    feishu: dict[str, Any], *, allow_disabled: bool = False
) -> dict[str, Any]:
    # allow_disabled: the provisioning flow verifies the freshly created target
    # BEFORE flipping `enabled` on, so it must bypass the readiness gate.
    if not feishu.get("enabled") and not allow_disabled:
        raise LarkCLIError("Feishu sync is disabled; complete Agent setup first", kind="config")
    identity = str(feishu.get("identity") or "user")
    verify_feishu_identity(feishu, identity=identity)
    fields = list_fields(feishu["base_token"], feishu["table_id"], identity=identity)
    mapping = resolve_field_mapping(fields, feishu.get("field_mapping"))
    return {
        "identity": identity,
        "field_count": len(fields),
        "mapping": {
            key: {
                "field_id": value["field_id"],
                "name": value["name"],
                "type": value["type"],
            }
            for key, value in mapping.items()
        },
        "resolved": mapping,
    }


def upsert_article(
    feishu: dict[str, Any],
    article: dict[str, Any],
    metadata: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    check = preflight_feishu(feishu)
    mapping = check["resolved"]
    record, skipped_fields = build_mapped_record(article, metadata, mapping)
    url_target = mapping["url"]
    url_field = url_target["field_id"] or url_target["name"]
    identity = check["identity"]
    # Match by the field NAME (stable inside one table) instead of the raw
    # field_id: lark-cli record filters accept names, and saved mappings may
    # carry ids that the filter endpoint does not resolve.
    record_id = find_record_by_url(
        feishu["base_token"],
        feishu["table_id"],
        str(record[url_field]),
        url_target["name"],
        identity=identity,
    )
    if record_id:
        # Updates must not clobber human-curated cells: 阅读状态 and 抓取时间
        # are only written on first creation.
        for logical in ("read_status", "fetched_at"):
            target = mapping.get(logical)
            if target:
                record.pop(target["field_id"] or target["name"], None)
    args = [
        "base",
        "+record-upsert",
        "--base-token",
        feishu["base_token"],
        "--table-id",
        feishu["table_id"],
        "--json",
        json.dumps(record, ensure_ascii=False),
        "--as",
        identity,
        "--format",
        "json",
    ]
    if record_id:
        args.extend(["--record-id", record_id])
    if dry_run:
        args.append("--dry-run")
    _run_lark(args)
    return {
        "updated": bool(record_id),
        "skipped_fields": skipped_fields,
    }
