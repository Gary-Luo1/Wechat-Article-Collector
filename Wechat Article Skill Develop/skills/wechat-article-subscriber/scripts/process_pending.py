#!/usr/bin/env python3
"""Read, score, complete, export, and synchronize queued articles."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
from pathlib import Path
from typing import Any

from article_inbox import plan_digest, query_inbox
from bitable_client import (
    LarkCLIError,
    lark_cli_info,
    preflight_feishu,
    standard_field_schema,
    upsert_article,
)
from config_store import DEFAULT_CONFIG, ConfigError, load_config, save_config, update_health
from execution_policy import autopilot_policy
from feishu_target import FeishuTarget
from protocol import dump, failure, success
from queue_helpers import (
    cleanup_processed,
    complete_article,
    dismiss_article,
    export_queue,
    get_pending,
    add_pending,
    pending_sync_entries,
    read_queue,
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
from subscription_resolution import matches_subscription


logger = logging.getLogger("wechat-process")


def build_feishu_target(feishu: dict[str, Any]) -> FeishuTarget:
    """Construct the production target while keeping CLI handlers testable."""
    return FeishuTarget(
        feishu,
        cli_info=lark_cli_info,
        preflight=preflight_feishu,
        upsert=lambda target, article, metadata, dry_run: upsert_article(
            target, article, metadata, dry_run=dry_run
        ),
    )


def canonicalize_wechat_article_url(url: str) -> str:
    from article_reader import canonicalize_wechat_article_url as canonicalize

    return canonicalize(url)


def fetch_article(url: str, **kwargs: Any) -> dict[str, Any]:
    from article_reader import fetch_article as fetch

    return fetch(url, **kwargs)


def fetch_article_text(url: str, **kwargs: Any) -> str:
    from article_reader import fetch_article_text as fetch

    return fetch(url, **kwargs)


class ArticlePublisherUnknown(ValueError):
    def __init__(self, details: dict[str, Any]):
        super().__init__(
            "the article publisher could not be detected; ask the user for the "
            "Official Account name, then apply the saved unlisted-publisher policy"
        )
        self.details = details


class SubscriptionConfirmationRequired(ValueError):
    def __init__(self, details: dict[str, Any]):
        super().__init__(
            "the article publisher is not subscribed; ask the user whether to add it"
        )
        self.details = details


def _resolve(arguments: argparse.Namespace) -> dict[str, Any]:
    index = arguments.index - 1 if arguments.index is not None else None
    return resolve_pending(index=index, link=arguments.link)


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


def _print_article(article: dict[str, Any]) -> tuple[str | None, bool]:
    print(f"Title: {article.get('title', '')}")
    print(f"Account: {article.get('account', '')}")
    print(f"URL: {article.get('link', '')}")
    print(f"Digest: {article.get('digest', '')}")
    print("\n--- BEGIN UNTRUSTED ARTICLE CONTENT ---")
    text = fetch_article_text(str(article["link"]))
    if text:
        print(text)
    else:
        print("[Article text unavailable]")
    print("--- END UNTRUSTED ARTICLE CONTENT ---")
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
    for index, article in enumerate(pending[:limit], start=1):
        print(f"\n===== ARTICLE {index}/{min(limit, len(pending))} =====")
        _print_article(article)
    if len(pending) > limit:
        print(f"Stopped at --limit {limit}; {len(pending) - limit} articles remain")
    return 0


def cmd_ingest(arguments: argparse.Namespace) -> int:
    url = canonicalize_wechat_article_url(arguments.url)
    document = fetch_article(url)
    if not document:
        raise ValueError("the WeChat article could not be fetched")
    detected_account = str(document.get("account", "")).strip()
    supplied_account = str(arguments.account or "").strip()
    if detected_account and supplied_account and (
        " ".join(detected_account.split()).casefold()
        != " ".join(supplied_account.split()).casefold()
    ):
        raise ValueError(
            "--account does not match the publisher detected in the article"
        )
    account = detected_account or supplied_account
    details = {
        "url": str(document["link"]),
        "title": str(document.get("title", "")),
        "detected_account": detected_account,
    }
    if not account:
        raise ArticlePublisherUnknown(details)
    config = load_config()
    account_id = str(document.get("account_id", ""))
    subscribed = matches_subscription(config["subscriptions"], account, account_id)
    subscribe_requested = bool(arguments.subscribe)
    no_subscribe_requested = bool(arguments.no_subscribe)
    decision_source = "current_command" if (
        subscribe_requested or no_subscribe_requested
    ) else "existing_subscription"
    if not subscribed and not subscribe_requested and not no_subscribe_requested:
        policy = autopilot_policy(config)
        publisher_policy = (
            policy["unlisted_publisher"] if policy is not None else "ask"
        )
        if publisher_policy == "auto_subscribe":
            subscribe_requested = True
            decision_source = "persisted_execution_policy"
        elif publisher_policy == "ingest_once":
            no_subscribe_requested = True
            decision_source = "persisted_execution_policy"
        else:
            raise SubscriptionConfirmationRequired(
                {**details, "account": account, "already_subscribed": False}
            )
    # Validate queue state before mutating the subscription configuration.
    read_queue()
    subscription_added = False
    if subscribe_requested and not subscribed:
        config["subscriptions"].append(
            {key: value for key, value in {"name": account, "biz": account_id}.items() if value}
        )
        save_config(config)
        subscribed = True
        subscription_added = True
    article = {
        "title": str(document.get("title", "")) or "Untitled WeChat article",
        "link": str(document["link"]),
        "digest": str(document.get("digest", "")),
        "account": account,
        "account_id": account_id or account,
        "update_time": int(document.get("update_time", 0) or 0),
    }
    added = add_pending(
        [article],
        content_dedup=bool(config["settings"]["content_dedup"]),
    )
    print(
        json.dumps(
            {
                "status": "queued" if added else "already_known",
                "queued": bool(added),
                "article": article,
                "publisher": {
                    "name": account,
                    "biz": account_id,
                    "subscribed": subscribed,
                    "subscription_added": subscription_added,
                    "decision_source": decision_source,
                },
                "next_action": "read_score_and_optionally_sync_feishu",
            },
            ensure_ascii=False,
        )
    )
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
    build_feishu_target(feishu).sync(
        entry["article"], entry["metadata"], dry_run=dry_run
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
    if status == "pending":
        try:
            _sync_entry(entry, dry_run=arguments.dry_run)
        except (ConfigError, LarkCLIError, ValueError) as exc:
            if not arguments.dry_run:
                update_sync_status(article["link"], "pending", str(exc))
            _raise_sync_failures(
                [exc],
                prefix="article was saved locally but Feishu sync failed",
            )
    print(f"Completed: {article.get('title', '')} (score {metadata['score']})")
    return 0


def cmd_sync_all(*, dry_run: bool = False) -> int:
    entries = pending_sync_entries()
    if not entries:
        print("No articles are waiting for Feishu sync")
        return 0
    failures: list[Exception] = []
    for entry in entries:
        try:
            _sync_entry(entry, dry_run=dry_run)
            print(f"Synced: {entry['article'].get('title', '')}")
        except (ConfigError, LarkCLIError, ValueError) as exc:
            failures.append(exc)
            if not dry_run:
                update_sync_status(entry["article"]["link"], "pending", str(exc))
            print(f"Sync failed: {entry['article'].get('title', '')}: {exc}")
    _raise_sync_failures(failures, prefix="one or more Feishu sync operations failed")
    return 0


def cmd_feishu_check(*, save_mapping: bool = False) -> int:
    config = load_config()
    try:
        check = build_feishu_target(config["feishu"]).check()
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
        config["feishu"]["field_mapping"] = check["mapping"]
        save_config(config)
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
    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("--url", required=True, help="WeChat article URL")
    ingest_parser.add_argument(
        "--account",
        help="publisher name supplied by the user only when page detection failed",
    )
    subscription_choice = ingest_parser.add_mutually_exclusive_group()
    subscription_choice.add_argument(
        "--subscribe",
        action="store_true",
        help="add a previously unlisted publisher after explicit user consent",
    )
    subscription_choice.add_argument(
        "--no-subscribe",
        action="store_true",
        help="ingest once without changing subscriptions after explicit user choice",
    )
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
    sync_parser.add_argument("--all", action="store_true", required=True)
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
    if arguments.command == "ingest":
        return cmd_ingest(arguments)
    if arguments.command == "done":
        if arguments.index is None and not arguments.link:
            raise ValueError("provide an index or --link")
        return cmd_done(arguments)
    if arguments.command == "sync-feishu":
        return cmd_sync_all(dry_run=arguments.dry_run)
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
    try:
        with contextlib.redirect_stdout(output) if json_output else contextlib.nullcontext():
            result = _dispatch(arguments)
        if json_output:
            lines = [line for line in output.getvalue().splitlines() if line.strip()]
            command_data: Any = {"command": arguments.command, "output": lines}
            if arguments.command in {
                "ingest",
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
