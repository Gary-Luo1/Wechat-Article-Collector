#!/usr/bin/env python3
"""Read, score, complete, export, and synchronize queued articles."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from article_inbox import plan_digest, query_inbox
from article_review import (
    ArticleContentIncompleteError,
    ArticleReadRequiredError,
    complete_review,
    raise_sync_failures as _raise_sync_failures,
    sync_entry as _sync_entry,
)
from article_review import require_complete_content as _review_require_complete_content
from bitable_client import (
    LarkCLIError,
    standard_field_schema,
)
from config_store import DEFAULT_CONFIG, ConfigError, load_config, update_health
from config_transitions import transition
from execution_policy import autopilot_policy
from feishu_target import production_feishu_target
from protocol import dump, failure, success
from queue_helpers import (
    cleanup_processed,
    dismiss_article,
    export_queue,
    get_pending,
    is_content_truncated,
    normalize_url,
    pending_sync_entries,
    read_queue,
    record_verified_read,
    resolve_pending,
    restore_dismissed,
    retire_legacy_pending,
    update_inbox_item,
    update_sync_status,
)
from scoring_rubric import is_advertisement

logger = logging.getLogger("wechat-process")

__all__ = ["ArticleContentIncompleteError", "ArticleReadRequiredError"]


class ArticleFetchPaidError(ValueError):
    """A paid content fetch failed; keeps the REDFOX_* code for the protocol."""

    def __init__(self, source: BaseException):
        super().__init__(f"redfox content fetch failed: {source}")
        self.code = getattr(source, "code", "REDFOX_API_ERROR")
        self.retryable = bool(getattr(source, "retryable", False))
        self.details = getattr(source, "details", None)


def _require_complete_content(article: dict[str, Any]) -> None:
    _review_require_complete_content(article)


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
    # Superseded by `inbox`; kept only so existing automation keeps working.
    print("Deprecated: use `process inbox` for the filtered, sortable view.", file=sys.stderr)
    pending = get_pending()
    matched = False
    print("--- BEGIN UNTRUSTED ARTICLE METADATA ---")
    for index, article in enumerate(pending, start=1):
        if account and article.get("account") != account:
            continue
        matched = True
        print(f"[{index}] {_metadata_text(article.get('title', ''), 512)}")
        print(f"    id: {article.get('id', '')}")
        print(f"    account: {_metadata_text(article.get('account', ''), 128)}")
        print(f"    url: {article.get('link', '')}")
    if not matched:
        print("No pending articles")
    print("--- END UNTRUSTED ARTICLE METADATA ---")
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
    print("--- BEGIN UNTRUSTED ARTICLE METADATA ---")
    for item in result["items"]:
        article = item["article"]
        marker = (
            f"pending #{item['pending_index']}"
            if item["status"] == "pending"
            else f"processed / {item.get('sync_status', '')}"
        )
        print(
            f"- [{marker}] {_metadata_text(article.get('title', ''), 512)} — "
            f"{_metadata_text(article.get('account', ''), 128)}"
        )
        print(f"  {article.get('link', '')}")
    print("--- END UNTRUSTED ARTICLE METADATA ---")
    return 0


def cmd_inbox_mark(arguments: argparse.Namespace) -> int:
    favorite: bool | None = None
    if arguments.favorite:
        favorite = True
    elif arguments.unfavorite:
        favorite = False
    state = "later" if arguments.later else ("active" if arguments.active else None)
    result = update_inbox_item(arguments.link, favorite=favorite, state=state)
    print(json.dumps(_metadata_result(result), ensure_ascii=False))
    return 0


def cmd_dismiss(arguments: argparse.Namespace) -> int:
    entry = dismiss_article(arguments.link)
    print(
        json.dumps(
            {
                "status": "dismissed",
                "reversible": True,
                "article": _metadata_article(entry["article"]),
                "trust_boundary": "untrusted_article_metadata",
                "restore_command": f"process restore --link {entry['article']['link']}",
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_restore(arguments: argparse.Namespace) -> int:
    article = restore_dismissed(arguments.link)
    print(
        json.dumps(
            {
                "status": "pending",
                "article": _metadata_article(article),
                "trust_boundary": "untrusted_article_metadata",
            },
            ensure_ascii=False,
        )
    )
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
    print("--- BEGIN UNTRUSTED ARTICLE METADATA ---")
    for index, item in enumerate(result["candidates"], start=1):
        print(
            f"{index}. {_metadata_text(item['title'], 512)} — "
            f"{_metadata_text(item['account'], 128)}"
        )
        print(f"   {item['url']}")
    print("--- END UNTRUSTED ARTICLE METADATA ---")
    return 0


def _metadata_text(value: Any, limit: int) -> str:
    from redfox_client import sanitize_text

    return sanitize_text(value, limit)


def _metadata_article(article: dict[str, Any]) -> dict[str, Any]:
    projected = {key: value for key, value in article.items() if key != "content"}
    projected["title"] = _metadata_text(projected.get("title", ""), 512)
    projected["account"] = _metadata_text(projected.get("account", ""), 128)
    projected["digest"] = _metadata_text(projected.get("digest", ""), 2048)
    return projected


def _metadata_result(result: dict[str, Any]) -> dict[str, Any]:
    projected = dict(result)
    article = projected.get("article")
    if isinstance(article, dict):
        projected["article"] = _metadata_article(article)
    projected["trust_boundary"] = "untrusted_article_metadata"
    return projected


def _load_article_text(article: dict[str, Any]) -> tuple[str, bool]:
    """Return (body, was_cached); fetches once via the paid detail endpoint.

    The fetched body is not written here: the caller persists it together with
    the verified-read proof in one queue transaction.
    """
    from redfox_client import RedfoxClient, clean_content

    cached = str(article.get("content") or "").strip()
    if cached:
        cleaned = clean_content(cached)
        if cleaned:
            return cleaned, True
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
    return text, False


def _print_article(article: dict[str, Any]) -> tuple[str, bool]:
    from redfox_client import RedfoxAPIError

    try:
        return _print_article_unprotected(article)
    except RedfoxAPIError as exc:
        # Keep the protocol envelope intact for automation (main() only catches
        # ValueError subclasses) while preserving the REDFOX code/retryable.
        raise ArticleFetchPaidError(exc) from exc


def _print_article_unprotected(article: dict[str, Any]) -> tuple[str, bool]:
    from redfox_client import sanitize_text

    text, was_cached = _load_article_text(article)
    # The nonce makes the untrusted-content boundary impossible to forge from
    # inside the body (a plain fixed marker could be echoed by a malicious
    # article to fake trusted trailing output).
    nonce = hashlib.sha256(
        (str(article["link"]) + str(time.time_ns())).encode()
    ).hexdigest()[:8]
    # One transaction stores the fetched body cache (paid-API economy) and the
    # verified-read proof together.
    saved = record_verified_read(
        str(article["link"]), text, content_to_cache=None if was_cached else text
    )
    print(f"\n--- BEGIN UNTRUSTED ARTICLE CONTENT {nonce} ---")
    print(f"Title: {sanitize_text(article.get('title', ''), 512)}")
    print(f"Account: {sanitize_text(article.get('account', ''), 128)}")
    print(f"URL: {article.get('link', '')}")
    print(f"Digest: {sanitize_text(article.get('digest', ''), 2048)}")
    print(text)
    print(f"--- END UNTRUSTED ARTICLE CONTENT {nonce} ---")
    if is_content_truncated(saved):
        print("Content coverage: incomplete (truncated); do not score, complete, or sync this article.")
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
        except (ValueError, LookupError, ConfigError) as exc:
            # LookupError: the article left the pending list mid-batch (another
            # process completed or dismissed it); keep the batch going.
            failures += 1
            print(f"[Article read failed: {exc}]")
    if len(pending) > limit:
        print(f"Stopped at --limit {limit}; {len(pending) - limit} articles remain")
    if failures:
        print(f"Batch read completed with {failures} failed article(s); {successful} succeeded")
        return 1
    return 0


def cmd_done(arguments: argparse.Namespace) -> int:
    outcome = complete_review(
        arguments,
        resolve=_resolve,
        sync=_sync_entry,
    )
    print(outcome["message"])
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
    # One preflight per batch: identity and field mapping do not change between
    # records, and each check spawns several slow lark-cli subprocess probes.
    preflight_result: dict[str, Any] | None = None
    for entry in entries:
        try:
            reused = _sync_entry(entry, dry_run=dry_run, preflight_result=preflight_result)
            preflight_result = reused or preflight_result
            print(f"Synced: {_metadata_text(entry['article'].get('title', ''), 512)}")
        except (ConfigError, KeyError, LarkCLIError, ValueError) as exc:
            failures.append(exc)
            if not dry_run:
                update_sync_status(entry["article"]["link"], "pending", str(exc))
            print(
                f"Sync failed: {_metadata_text(entry['article'].get('title', ''), 512)}: {exc}"
            )
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
        config = transition("feishu_mapping", check["mapping"])["config"]
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
    list_parser = commands.add_parser(
        "list", help="deprecated: use `inbox` for the filtered, sortable view"
    )
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
        if arguments.all and arguments.link:
            raise ValueError("choose either --all or --link <URL>, not both")
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
    try:
        retired = retire_legacy_pending()
        if retired:
            logger.info(
                "retired %d pre-redfox queue entr%s (no body and no work_uuid)",
                retired,
                "y" if retired == 1 else "ies",
            )
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
    except Exception as exc:
        # Unexpected failures (corrupt queue, lock timeout, OS errors) must
        # still produce a protocol envelope instead of a raw traceback.
        if json_output:
            print(dump(failure(exc)))
        elif isinstance(exc, (ConfigError, LarkCLIError, LookupError, ValueError)):
            logger.error("%s", exc)
        else:
            logger.exception("unexpected failure in %s", arguments.command)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
