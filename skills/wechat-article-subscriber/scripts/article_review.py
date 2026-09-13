"""Complete one reviewed article through the durable local workflow.

The process command owns argparse and output formatting.  This module owns the
ordered completion decision: content/read preconditions, score and policy,
queue transition, optional Feishu sync, and local retry compensation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from config_store import ConfigError, load_config
from execution_policy import autopilot_policy
from feishu_target import production_feishu_target
from lark_runtime import LarkCLIError
from queue_helpers import (
    complete_article,
    has_verified_read,
    is_content_truncated,
    update_sync_status,
)
from redfox_client import sanitize_text
from scoring_rubric import calculate_score, format_rationale, should_sync


class ArticleReadRequiredError(ValueError):
    """A scoreable article must have been read in a prior command invocation."""

    code = "ARTICLE_READ_REQUIRED"
    retryable = False
    next_action = "read_article_before_completion"

    def __init__(self) -> None:
        super().__init__("read the article successfully before scoring or completing it")


class ArticleContentIncompleteError(ValueError):
    code = "ARTICLE_CONTENT_INCOMPLETE"
    retryable = False
    next_action = "review_incomplete_article_or_dismiss"

    def __init__(self) -> None:
        super().__init__(
            "article content is truncated; keep pending for partial review or dismiss it; "
            "scoring and Feishu sync require complete content, and rereading this cache will not restore it"
        )


def require_complete_content(article: dict[str, Any]) -> None:
    if is_content_truncated(article):
        raise ArticleContentIncompleteError()


def _score_metadata(arguments: Any) -> dict[str, Any]:
    if arguments.dims_file:
        try:
            raw = arguments.dims_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"cannot read --dims-file: {exc}") from exc
        source = "--dims-file"
    elif arguments.dims:
        raw = arguments.dims
        source = "--dims"
    else:
        raise ValueError("provide all five dimension scores with --dims or --dims-file")
    try:
        dimensions = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc}") from exc
    return {
        "score": calculate_score(dimensions),
        "dimensions": dimensions,
        "summary": arguments.summary.strip(),
        "rationale": (arguments.rationale or format_rationale(dimensions)).strip(),
        "tags": [item.strip() for item in arguments.tags.split(",") if item.strip()],
        "ad": False,
    }


def sync_entry(
    entry: dict[str, Any],
    *,
    dry_run: bool = False,
    preflight_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Sync one complete entry and return reusable batch preflight data."""
    require_complete_content(entry["article"])
    feishu = load_config()["feishu"]
    if not feishu["enabled"]:
        raise ConfigError("Feishu sync is disabled; complete Agent setup first")
    result = (
        production_feishu_target(feishu).sync(
            entry["article"],
            entry["metadata"],
            dry_run=dry_run,
            preflight_result=preflight_result,
        )
        or {}
    )
    if result.get("skipped_fields"):
        print(
            "⚠ 部分字段因选项不匹配被跳过（在飞书补选项后重同步即可）："
            + "、".join(map(str, result["skipped_fields"]))
        )
    if not dry_run:
        update_sync_status(entry["article"]["link"], "synced")
    preflight = result.get("preflight")
    return preflight if isinstance(preflight, dict) else None


def raise_sync_failures(failures: list[Exception], *, prefix: str) -> None:
    """Preserve the first non-retryable failure classification for automation."""
    if not failures:
        return
    primary = next(
        (item for item in failures if not bool(getattr(item, "retryable", False))),
        failures[0],
    )
    message = f"{prefix}; {len(failures)} item(s) remain pending; first failure: {primary}"
    if isinstance(primary, LarkCLIError):
        raise LarkCLIError(
            message,
            kind=primary.kind,
            code=primary.code,
            retryable=all(bool(getattr(item, "retryable", False)) for item in failures),
        ) from primary
    if isinstance(primary, ConfigError):
        raise ConfigError(message) from primary
    if isinstance(primary, ArticleContentIncompleteError):
        raise primary
    raise ValueError(message) from primary


def complete_review(
    arguments: Any,
    *,
    resolve: Callable[[Any], dict[str, Any]],
    sync: Callable[..., Any] = sync_entry,
) -> dict[str, Any]:
    """Apply the completion state machine through its two external boundaries."""
    if arguments.force_feishu and not arguments.feishu:
        raise ValueError("--force-feishu requires --feishu")
    article = resolve(arguments)
    require_complete_content(article)
    if arguments.ad:
        if arguments.dry_run and not arguments.feishu:
            raise ValueError("--dry-run is only valid together with --feishu")
        if arguments.dry_run:
            return {
                "message": "Dry run: advertisement remains pending: "
                f"{sanitize_text(article.get('title', ''), 512)}",
                "status": "dry_run",
            }
        complete_article(
            article["link"],
            {"ad": True, "reason": "advertisement/promotion"},
            sync_status="skipped_ad",
        )
        return {
            "message": f"Skipped advertisement: {sanitize_text(article.get('title', ''), 512)}",
            "status": "skipped_ad",
        }
    if not has_verified_read(article):
        raise ArticleReadRequiredError()
    try:
        config = load_config()
    except ConfigError:
        if arguments.feishu:
            raise
        config = None
    policy = autopilot_policy(config) if config is not None else None
    policy_sync = bool(
        config is not None
        and policy is not None
        and policy["allow_feishu_sync"]
        and config["feishu"]["enabled"]
    )
    if arguments.dry_run and not (arguments.feishu or policy_sync):
        raise ValueError("--dry-run is only valid together with --feishu")
    metadata = _score_metadata(arguments)
    metadata["content_source"] = str(article.get("content_source") or "direct")
    sync_requested = bool(arguments.feishu or policy_sync)
    if sync_requested:
        if config is None:
            raise ConfigError("Feishu sync requires configuration")
        if arguments.force_feishu or should_sync(
            metadata["score"], config["settings"]["min_score"]
        ):
            status = "pending"
        else:
            status = "skipped_low_score"
    else:
        status = "not_requested"
    if arguments.dry_run:
        if status != "pending":
            return {
                "message": f"Dry run: score {metadata['score']} is below the configured Feishu threshold",
                "status": status,
                "score": metadata["score"],
            }
        sync({"article": article, "metadata": metadata}, dry_run=True)
        return {
            "message": "Dry run succeeded; article remains pending: "
            f"{sanitize_text(article.get('title', ''), 512)}",
            "status": "dry_run",
            "score": metadata["score"],
        }
    entry = complete_article(article["link"], metadata, sync_status=status)
    if status == "pending":
        # complete_article refuses dismissed entries atomically, so a race with
        # dismiss surfaces as a LookupError from that call instead.
        try:
            sync(entry)
        except (ConfigError, KeyError, LarkCLIError, ValueError) as exc:
            update_sync_status(article["link"], "pending", str(exc))
            raise_sync_failures(
                [exc],
                prefix="article was saved locally but Feishu sync failed",
            )
    if status == "skipped_low_score" and config is not None:
        sync_note = (
            f"未同步：{metadata['score']} 低于阈值"
            f"（{config['settings']['min_score']}）；确需同步可加 --force-feishu 重评"
        )
    else:
        sync_note = {
            "synced": "已同步到飞书",
            "pending": "已入同步队列",
            "not_requested": "未请求飞书同步（加 --feishu 可同步；批量自动化需先确认执行策略）",
            "skipped_ad": "已按广告跳过",
        }.get(status, status)
    return {
        "message": f"Completed: {sanitize_text(article.get('title', ''), 512)} "
        f"(score {metadata['score']}) | 同步: {sync_note}",
        "status": status,
        "score": metadata["score"],
    }
