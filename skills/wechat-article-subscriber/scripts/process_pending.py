#!/usr/bin/env python3
"""Read, score, complete, export, and synchronize queued articles."""

from __future__ import annotations

import argparse
import contextlib
from copy import deepcopy
import hashlib
import io
import json
import logging
import time
from pathlib import Path
from typing import Any

from article_inbox import plan_digest, query_inbox
from bitable_client import (
    LarkCLIError,
    standard_field_schema,
)
from config_store import DEFAULT_CONFIG, ConfigError, load_config, modify_config, update_health
from execution_policy import autopilot_policy, invalidate_for_feishu_change
from feishu_target import production_feishu_target
from protocol import dump, failure, success
from queue_helpers import (
    cache_article_content,
    normalize_url,
    read_queue,
    cleanup_processed,
    retire_legacy_pending,
    complete_article,
    dismiss_article,
    export_queue,
    get_pending,
    has_verified_read,
    pending_sync_entries,
    record_verified_read,
    resolve_pending,
    restore_dismissed,
    update_inbox_item,
    update_sync_status,
)
from scoring_rubric import (
    calculate_score,
    format_rationale,
    is_advertisement,
    should_sync,
)


logger = logging.getLogger("wechat-process")


class ArticleFetchPaidError(ValueError):
    """A paid content fetch failed; keeps the REDFOX_* code for the protocol."""

    def __init__(self, source: BaseException):
        super().__init__(f"redfox content fetch failed: {source}")
        self.code = getattr(source, "code", "REDFOX_API_ERROR")
        self.retryable = bool(getattr(source, "retryable", False))
        self.details = getattr(source, "details", None)


class ArticleReadRequiredError(ValueError):
    """A scoreable article must have been read in a prior command invocation."""

    code = "ARTICLE_READ_REQUIRED"
    retryable = False
    next_action = "read_article_before_completion"

    def __init__(self) -> None:
        super().__init__("read the article successfully before scoring or completing it")


def _resolve(arguments: argparse.Namespace) -> dict[str, Any]:
    index = arguments.index - 1 if arguments.index is not None else None
    try:
        return resolve_pending(index=index, link=arguments.link)
    except LookupError:
        if arguments.link:
            processed = read_queue()["processed"].get(normalize_url(arguments.link))
            if processed:
                disposition = (processed.get("metadata") or {}).get("disposition")
                if disposition in {"dismissed", "legacy_unreadable"}:
                    raise LookupError(
                        f"article is already processed ({disposition}); restore it "
                        "first if you want it back in the workflow"
                    ) from None
                raise LookupError(
                    "article is already processed (sync_status="
                    f"{processed.get('sync_status')}); use sync-feishu --link to "
                    "re-sync it"
                ) from None
        raise


def cmd_list(account: str | None = None) -> int:
    pending = get_pending()
    selected = [item for item in pending if not account or item.get("account") == account]
    if not selected:
        print("No pending articles")
        return 0
    for index, article in enumerate(pending, start=1):
        if article not in selected:
            continue
        print(f"[{index}] {article.get('title', '')}")
        print(f"    id: {article.get('id', '')}")
        print(f"    account: {article.get('account', '')}")
        print(f"    url: {article.get('link', '')}")
    return 0


def cmd_inbox(arguments: argparse.Namespace) -> int:
    result = query_inbox(
        status=arguments.status,
        account=arguments.account or "",
        query=arguments.query or "",
        sort=arguments.sort,
        limit=arguments.limit,
        favorite=arguments.favorite,
        state=arguments.state,
        disposition=arguments.disposition,
    )
    if arguments.format == "json":
        print(json.dumps(result, ensure_ascii=False))
        return 0
    summary = result["summary"]
    print(
        f"Inbox: {summary['pending']} pending, {summary['processed']} processed, "
        f"{summary['sync_pending']} waiting for sync"
    )
    if not result["items"]:
        print("No articles match the current filters")
        return 0
    for item in result["items"]:
        article = item["article"]
        marker = (
            f"pending #{item['pending_index']}"
            if item["status"] == "pending"
            else f"processed / {item.get('sync_status', '')}"
        )
        print(f"- [{marker}] {article.get('title', '')} — {article.get('account', '')}")
        print(f"  {article.get('link', '')}")
    return 0


def cmd_inbox_mark(arguments: argparse.Namespace) -> int:
    favorite: bool | None = None
    if arguments.favorite:
        favorite = True
    elif arguments.unfavorite:
        favorite = False
    state = "later" if arguments.later else ("active" if arguments.active else None)
    result = update_inbox_item(arguments.link, favorite=favorite, state=state)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_dismiss(arguments: argparse.Namespace) -> int:
    entry = dismiss_article(arguments.link)
    print(
        json.dumps(
            {
                "status": "dismissed",
                "reversible": True,
                "article": entry["article"],
                "restore_command": f"process restore --link {entry['article']['link']}",
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_restore(arguments: argparse.Namespace) -> int:
    article = restore_dismissed(arguments.link)
    print(json.dumps({"status": "pending", "article": article}, ensure_ascii=False))
    return 0


def _digest_plan(arguments: argparse.Namespace) -> dict[str, Any]:
    try:
        preferences = load_config()["preferences"]
    except ConfigError:
        preferences = dict(DEFAULT_CONFIG["preferences"])
    hours = arguments.hours if arguments.hours is not None else preferences["digest_hours"]
    limit = arguments.limit if arguments.limit is not None else preferences["digest_limit"]
    return plan_digest(
        preferences,
        hours=hours,
        limit=limit,
        include_later=arguments.include_later,
    )


def cmd_digest_plan(arguments: argparse.Namespace) -> int:
    result = _digest_plan(arguments)
    if arguments.format == "json":
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(
        f"Digest candidates: {result['returned']} of {result['eligible']} eligible "
        f"within {result['window_hours']} hours"
    )
    for index, item in enumerate(result["candidates"], start=1):
        print(f"{index}. {item['title']} — {item['account']}")
        print(f"   {item['url']}")
    return 0


def _load_article_text(article: dict[str, Any]) -> str:
    """Return the cached body, or fetch it once via the paid detail endpoint."""
    cached = str(article.get("content") or "").strip()
    if cached:
        return cached
    work_uuid = str(article.get("work_uuid") or "").strip()
    if not work_uuid:
        raise ValueError(
            "this queued article has neither a cached body nor a redfox "
            "work_uuid; re-run discover, or dismiss it"
        )
    config = load_config()
    api_key = config["redfox"]["api_key"].strip()
    if not api_key:
        raise ConfigError("redfox API key is missing; run the redfox key setup command")
    from redfox_client import RedfoxClient, clean_content

    client = RedfoxClient(api_key)
    try:
        detail, api_code = client.query_work(work_uuid)
        text = clean_content(detail.get("content"))
    finally:
        client.close()
    if not text:
        if api_code == 3203:
            raise ValueError(
                "the redfox library has not crawled this article's body yet; "
                "retry after a later sync cycle or dismiss it"
            )
        raise ValueError(
            "redfox returned no content for this article; dismiss it or contact "
            "the data source — retrying will not help"
        )
    # Cache so re-reads never pay for the same body twice.
    cache_article_content(str(article["link"]), text)
    return text


def _print_article(article: dict[str, Any]) -> tuple[str, bool]:
    from redfox_client import RedfoxAPIError

    try:
        return _print_article_unprotected(article)
    except RedfoxAPIError as exc:
        # Keep the protocol envelope intact for automation (main() only catches
        # ValueError subclasses) while preserving the REDFOX code/retryable.
        raise ArticleFetchPaidError(exc) from exc


def _print_article_unprotected(article: dict[str, Any]) -> tuple[str, bool]:
    print(f"Title: {article.get('title', '')}")
    print(f"Account: {article.get('account', '')}")
    print(f"URL: {article.get('link', '')}")
    print(f"Digest: {article.get('digest', '')}")
    text = _load_article_text(article)
    # The nonce makes the untrusted-content boundary impossible to forge from
    # inside the body (a plain fixed marker could be echoed by a malicious
    # article to fake trusted trailing output).
    nonce = hashlib.sha256(
        (str(article["link"]) + str(time.time_ns())).encode()
    ).hexdigest()[:8]
    print(f"\n--- BEGIN UNTRUSTED ARTICLE CONTENT {nonce} ---")
    record_verified_read(str(article["link"]), text)
    print(text)
    print(f"--- END UNTRUSTED ARTICLE CONTENT {nonce} ---")
    print(f"Content source: {article.get('content_source') or 'direct'}")
    suspected = is_advertisement(str(article.get("title", "")), text or "")
    print(f"Ad heuristic: {'suspected' if suspected else 'not detected'}")
    return text, suspected


def cmd_read(arguments: argparse.Namespace) -> int:
    _print_article(_resolve(arguments))
    return 0


def cmd_batch_read(limit: int) -> int:
    pending = get_pending()
    if not pending:
        print("No pending articles")
        return 0
    requested = min(limit, len(pending))
    successful = 0
    failures = 0
    for index, article in enumerate(pending[:limit], start=1):
        print(f"\n===== ARTICLE {index}/{requested} =====")
        try:
            _print_article(article)
            successful += 1
        except (ValueError, ConfigError) as exc:
            failures += 1
            print(f"[Article read failed: {exc}]")
    if len(pending) > limit:
        print(f"Stopped at --limit {limit}; {len(pending) - limit} articles remain")
    if failures:
        print(f"Batch read completed with {failures} failed article(s); {successful} succeeded")
        return 1
    return 0


def _read_dimensions(arguments: argparse.Namespace) -> Any:
    if arguments.dims_file:
        try:
            # PowerShell 5.1 Out-File -Encoding UTF8 adds a BOM. utf-8-sig
            # accepts both BOM and normal UTF-8 without weakening JSON parsing.
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
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc}") from exc


def _score_metadata(arguments: argparse.Namespace) -> dict[str, Any]:
    dimensions = _read_dimensions(arguments)
    score = calculate_score(dimensions)
    rationale = arguments.rationale or format_rationale(dimensions)
    tags = [item.strip() for item in arguments.tags.split(",") if item.strip()]
    return {
        "score": score,
        "dimensions": dimensions,
        "summary": arguments.summary.strip(),
        "rationale": rationale.strip(),
        "tags": tags,
        "ad": False,
    }


def _sync_entry(entry: dict[str, Any], *, dry_run: bool = False) -> None:
    config = load_config()
    feishu = config["feishu"]
    if not feishu["enabled"]:
        raise ConfigError("Feishu sync is disabled; complete Agent setup first")
    result = production_feishu_target(feishu).sync(
        entry["article"], entry["metadata"], dry_run=dry_run
    ) or {}
    if result.get("skipped_fields"):
        print(
            "⚠ 部分字段因选项不匹配被跳过（在飞书补选项后重同步即可）："
            + "、".join(map(str, result["skipped_fields"]))
        )
    if not dry_run:
        update_sync_status(entry["article"]["link"], "synced")


def _raise_sync_failures(failures: list[Exception], *, prefix: str) -> None:
    """Preserve the first non-retryable failure classification for automation."""
    if not failures:
        return
    primary = next(
        (
            item
            for item in failures
            if not bool(getattr(item, "retryable", False))
        ),
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
    raise ValueError(message) from primary


def cmd_done(arguments: argparse.Namespace) -> int:
    if arguments.force_feishu and not arguments.feishu:
        raise ValueError("--force-feishu requires --feishu")
    article = _resolve(arguments)
    if arguments.ad:
        if arguments.dry_run and not arguments.feishu:
            raise ValueError("--dry-run is only valid together with --feishu")
        if arguments.dry_run:
            print(f"Dry run: advertisement remains pending: {article.get('title', '')}")
            return 0
        complete_article(
            article["link"],
            {"ad": True, "reason": "advertisement/promotion"},
            sync_status="skipped_ad",
        )
        print(f"Skipped advertisement: {article.get('title', '')}")
        return 0
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
            print(
                f"Dry run: score {metadata['score']} is below the configured Feishu threshold"
            )
            return 0
        _sync_entry({"article": article, "metadata": metadata}, dry_run=True)
        print(f"Dry run succeeded; article remains pending: {article.get('title', '')}")
        return 0
    entry = complete_article(article["link"], metadata, sync_status=status)
    if entry.get("metadata", {}).get("disposition") == "dismissed":
        raise LookupError("article was dismissed and cannot be completed")
    if status == "pending":
        try:
            _sync_entry(entry, dry_run=arguments.dry_run)
        except (ConfigError, KeyError, LarkCLIError, ValueError) as exc:
            if not arguments.dry_run:
                update_sync_status(article["link"], "pending", str(exc))
            _raise_sync_failures(
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
    print(f"Completed: {article.get('title', '')} (score {metadata['score']}) | 同步: {sync_note}")
    return 0


def cmd_sync_all(*, dry_run: bool = False, link: str | None = None) -> int:
    if link:
        data = read_queue()
        entry = data["processed"].get(normalize_url(link))
        if not entry:
            raise LookupError("no processed article matches that URL")
        disposition = (entry.get("metadata") or {}).get("disposition")
        if disposition in {"dismissed", "legacy_unreadable"}:
            raise ValueError(
                f"this entry is {disposition} and has no score/summary to sync; "
                "restore it and complete it properly first"
            )
        if not dry_run and entry.get("sync_status") != "pending":
            update_sync_status(link, "pending")
        entries = [
            {"article": entry["article"], "metadata": entry.get("metadata", {})}
        ]
    else:
        entries = pending_sync_entries()
    if not entries:
        print("No articles are waiting for Feishu sync")
        return 0
    failures: list[Exception] = []
    for entry in entries:
        try:
            _sync_entry(entry, dry_run=dry_run)
            print(f"Synced: {entry['article'].get('title', '')}")
        except (ConfigError, KeyError, LarkCLIError, ValueError) as exc:
            failures.append(exc)
            if not dry_run:
                update_sync_status(entry["article"]["link"], "pending", str(exc))
            print(f"Sync failed: {entry['article'].get('title', '')}: {exc}")
    _raise_sync_failures(failures, prefix="one or more Feishu sync operations failed")
    return 0


def cmd_feishu_check(*, save_mapping: bool = False) -> int:
    config = load_config()
    try:
        check = production_feishu_target(config["feishu"]).check()
    except Exception as exc:
        try:
            update_health(
                "feishu",
                success=False,
                failure_kind=getattr(exc, "kind", type(exc).__name__),
            )
        except ConfigError:
            pass
        raise
    if save_mapping:
        def mutate_mapping(config: dict[str, Any]) -> dict[str, Any]:
            previous = deepcopy(config["feishu"])
            config["feishu"]["field_mapping"] = check["mapping"]
            invalidate_for_feishu_change(config, previous, config["feishu"])
            return config

        config = modify_config(mutate_mapping)
    update_health("feishu", success=True)
    print(
        json.dumps(
            {
                "ok": True,
                "identity": check["identity"],
                "field_count": check["field_count"],
                "field_mapping": check["mapping"],
                "mapping_saved": save_mapping,
                "note": (
                    "Read-only checks passed. Qualified writes may continue under the "
                    "persisted execution policy."
                    if autopilot_policy(config)
                    and config["setup"]["execution_policy"]["allow_feishu_sync"]
                    else (
                        "Read-only checks passed. A real write requires current user "
                        "authorization or an approved execution policy."
                    )
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_feishu_schema() -> int:
    print(json.dumps(standard_field_schema(), ensure_ascii=False))
    return 0


def _add_selector(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("index", type=int, nargs="?", help="1-based pending index")
    parser.add_argument("--link", help="stable article URL; preferred for automation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--account")
    inbox_parser = commands.add_parser("inbox")
    inbox_parser.add_argument("--status", choices=("pending", "processed", "all"), default="pending")
    inbox_parser.add_argument("--account")
    inbox_parser.add_argument("--query")
    inbox_parser.add_argument("--sort", choices=("newest", "oldest"), default="newest")
    inbox_parser.add_argument("--limit", type=int, default=20)
    inbox_parser.add_argument("--favorite", action="store_true")
    inbox_parser.add_argument("--state", choices=("active", "later", "all"), default="all")
    inbox_parser.add_argument(
        "--disposition",
        choices=("completed", "dismissed", "all"),
        default="all",
    )
    mark_parser = commands.add_parser("inbox-mark")
    mark_parser.add_argument("--link", required=True)
    favorite_choice = mark_parser.add_mutually_exclusive_group()
    favorite_choice.add_argument("--favorite", action="store_true")
    favorite_choice.add_argument("--unfavorite", action="store_true")
    state_choice = mark_parser.add_mutually_exclusive_group()
    state_choice.add_argument("--later", action="store_true")
    state_choice.add_argument("--active", action="store_true")
    dismiss_parser = commands.add_parser("dismiss")
    dismiss_parser.add_argument("--link", required=True)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--link", required=True)
    digest_parser = commands.add_parser("digest-plan")
    digest_parser.add_argument("--hours", type=int)
    digest_parser.add_argument("--limit", type=int)
    digest_parser.add_argument("--include-later", action="store_true")
    read_parser = commands.add_parser("read")
    _add_selector(read_parser)
    batch_parser = commands.add_parser("batch-read")
    batch_parser.add_argument("--limit", type=int, default=10)
    done_parser = commands.add_parser("done")
    _add_selector(done_parser)
    done_parser.add_argument("--ad", action="store_true")
    dimensions = done_parser.add_mutually_exclusive_group()
    dimensions.add_argument("--dims", help="JSON object containing exactly five dimensions")
    dimensions.add_argument(
        "--dims-file",
        type=Path,
        help="UTF-8 JSON file containing exactly five dimensions",
    )
    done_parser.add_argument("--summary", default="")
    done_parser.add_argument("--rationale", default="")
    done_parser.add_argument("--tags", default="")
    done_parser.add_argument("--feishu", action="store_true")
    done_parser.add_argument(
        "--force-feishu",
        action="store_true",
        help="honor an explicit single-article write request even below the score threshold",
    )
    done_parser.add_argument("--dry-run", action="store_true")
    sync_parser = commands.add_parser("sync-feishu")
    sync_parser.add_argument("--all", action="store_true")
    sync_parser.add_argument("--link", default="", help="re-sync one processed article by URL")
    sync_parser.add_argument("--dry-run", action="store_true")
    check_parser = commands.add_parser("feishu-check")
    check_parser.add_argument("--save-mapping", action="store_true")
    commands.add_parser("feishu-schema")
    export_parser = commands.add_parser("export")
    export_parser.add_argument("path", type=Path)
    clean_parser = commands.add_parser("clean")
    clean_parser.add_argument("--days", type=int, default=365)
    return parser


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "list":
        return cmd_list(arguments.account)
    if arguments.command == "inbox":
        if arguments.limit < 1 or arguments.limit > 100:
            raise ValueError("--limit must be between 1 and 100")
        return cmd_inbox(arguments)
    if arguments.command == "inbox-mark":
        if not any(
            (arguments.favorite, arguments.unfavorite, arguments.later, arguments.active)
        ):
            raise ValueError("choose favorite/unfavorite and/or later/active")
        return cmd_inbox_mark(arguments)
    if arguments.command == "dismiss":
        return cmd_dismiss(arguments)
    if arguments.command == "restore":
        return cmd_restore(arguments)
    if arguments.command == "digest-plan":
        return cmd_digest_plan(arguments)
    if arguments.command == "read":
        return cmd_read(arguments)
    if arguments.command == "batch-read":
        if arguments.limit < 1 or arguments.limit > 100:
            raise ValueError("--limit must be between 1 and 100")
        return cmd_batch_read(arguments.limit)
    if arguments.command == "done":
        if arguments.index is None and not arguments.link:
            raise ValueError("provide an index or --link")
        return cmd_done(arguments)
    if arguments.command == "sync-feishu":
        if not arguments.all and not arguments.link:
            raise ValueError("choose --all or --link <URL>")
        return cmd_sync_all(dry_run=arguments.dry_run, link=arguments.link or None)
    if arguments.command == "feishu-check":
        return cmd_feishu_check(save_mapping=arguments.save_mapping)
    if arguments.command == "feishu-schema":
        return cmd_feishu_schema()
    if arguments.command == "export":
        print(export_queue(arguments.path))
        return 0
    if arguments.command == "clean":
        print(f"Removed {cleanup_processed(arguments.days)} old records")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    json_output = arguments.format == "json"
    output = io.StringIO()
    retired = retire_legacy_pending()
    if retired:
        logger.info(
            "retired %d pre-redfox queue entr%s (no body and no work_uuid)",
            retired,
            "y" if retired == 1 else "ies",
        )
    try:
        with contextlib.redirect_stdout(output) if json_output else contextlib.nullcontext():
            result = _dispatch(arguments)
        if json_output:
            lines = [line for line in output.getvalue().splitlines() if line.strip()]
            command_data: Any = {"command": arguments.command, "output": lines}
            if arguments.command in {
                "inbox",
                "inbox-mark",
                "dismiss",
                "restore",
                "digest-plan",
            } and len(lines) == 1:
                try:
                    command_data = json.loads(lines[0])
                except json.JSONDecodeError:
                    pass
            next_action = "none" if result == 0 else "inspect_failed_items"
            if (
                arguments.command == "digest-plan"
                and isinstance(command_data, dict)
                and command_data.get("candidates")
            ):
                next_action = "read_score_digest_candidates"
            envelope = success(
                command_data,
                next_action=next_action,
            )
            if result:
                envelope["ok"] = False
                envelope["error"] = {
                    "code": "COMMAND_PARTIAL_FAILURE",
                    "message": "one or more items failed",
                    "retryable": True,
                    "next_action": "inspect_failed_items",
                }
            print(dump(envelope))
        return result
    except (ConfigError, LarkCLIError, LookupError, ValueError) as exc:
        if json_output:
            print(dump(failure(exc)))
        else:
            logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
