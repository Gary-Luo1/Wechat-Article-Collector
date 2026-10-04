#!/usr/bin/env python3
"""Discover recent articles via the redfox API and append them to the queue."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from config_store import ConfigError, load_config, modify_config
from protocol import dump, failure, success
from queue_helpers import (
    add_pending,
    cache_pending_body,
    cleanup_processed,
    expire_stale_pending,
    get_pending,
    mark_body_status,
    read_queue,
)
from redfox_client import RedfoxAPIError, RedfoxAuthError, RedfoxClient


logger = logging.getLogger("wechat-discover")

# max_articles_per_account is the collection cap, not a page size to retry past.
UNCRAWLED_RETRY_LIMIT = 20


def _subscription_cooldown_active(
    subscription: dict,
    interval_hours: float,
    requested_hours: float | None = None,
) -> bool:
    """Skip a paid list only when a recent fetch already covered this window.

    ``interval_hours`` is the billing cooldown (``check_hours``). A shorter
    manual lookback must not consume that cooldown: the next full-window run
    would otherwise skip the account and permanently miss the gap. Rows saved
    before the covered window was recorded are treated as a full-interval fetch.
    """
    raw = str(subscription.get("last_discovered_at", "")).strip()
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    if elapsed >= interval_hours * 3600:
        return False
    requested = interval_hours if requested_hours is None else float(requested_hours)
    covered = subscription.get("last_discovered_window_hours")
    if covered is None or covered == "":
        covered_hours = interval_hours
    else:
        try:
            covered_hours = float(covered)
        except (TypeError, ValueError):
            return False
    return covered_hours + 1e-9 >= requested


def _mark_subscription_discovered(
    identity: tuple[str, str, str],
    config_path: Path | None,
    window_hours: float,
) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def mutate(saved: dict) -> dict:
        for sub in saved["subscriptions"]:
            if (
                str(sub.get("name", "")).strip(),
                str(sub.get("alias", "")).strip(),
                str(sub.get("biz", "")).strip(),
            ) == identity:
                sub["last_discovered_at"] = now
                sub["last_discovered_window_hours"] = float(window_hours)
                sub.pop("discovery_partial_runs", None)
                return saved
        # A concurrent edit changed the subscription identity; without the
        # timestamp the paid-call cooldown cannot apply next cycle.
        logger.warning(
            "subscription %s changed during discovery; cooldown timestamp not saved",
            "/".join(part or "-" for part in identity),
        )
        return saved

    modify_config(mutate, path=config_path)


def _queued_links() -> set[str]:
    """Links already stored, so a later partial pass can continue past them."""
    links: set[str] = set()
    queue = read_queue()
    for article in queue["pending"]:
        for key in ("link", "normalized_url"):
            value = str(article.get(key) or "").strip()
            if value:
                links.add(value)
    for entry in queue["processed"].values():
        article = entry.get("article") if isinstance(entry, dict) else None
        if not isinstance(article, dict):
            continue
        for key in ("link", "normalized_url"):
            value = str(article.get(key) or "").strip()
            if value:
                links.add(value)
    return links


def pending_expiry_hours(config: dict) -> float:
    """Keep unread leftovers for two lookback windows, and at least 48 hours."""
    window = max(
        float(config["settings"]["check_hours"]),
        float(config["preferences"]["digest_hours"]),
    )
    return max(window * 2, 48)


def retry_uncrawled_bodies(api_key: str, request_delay: float) -> dict[str, int]:
    """Retry pending bodies the library had not crawled. Code 3203 is unpaid."""
    from redfox_client import RedfoxClient, clean_content

    summary = {"retried": 0, "ready": 0, "still_uncrawled": 0}
    targets = [
        article
        for article in get_pending()
        if article.get("body_status") == "uncrawled"
        and not str(article.get("content") or "").strip()
        and str(article.get("work_uuid") or "").strip()
    ][:UNCRAWLED_RETRY_LIMIT]
    if not targets or not str(api_key or "").strip():
        return summary
    client = RedfoxClient(str(api_key).strip(), request_delay=float(request_delay))
    try:
        for article in targets:
            summary["retried"] += 1
            detail, api_code = client.query_work(str(article["work_uuid"]))
            text = clean_content(detail.get("content"))
            if text and cache_pending_body(str(article["link"]), text):
                summary["ready"] += 1
            elif api_code == 3203:
                summary["still_uncrawled"] += 1
            else:
                mark_body_status(str(article["link"]), "unavailable")
    finally:
        client.close()
    return summary


def discover_articles(
    config: dict,
    hours: float,
    config_path: Path | None = None,
    diagnostics: list[dict] | None = None,
    on_account_articles: Callable[[list[dict]], int] | None = None,
    force: bool = False,
) -> list[dict]:
    """Discover articles through the paid redfox API (billing-aware)."""
    api_key = config["redfox"]["api_key"].strip()
    if not api_key:
        raise ConfigError("redfox API key is missing; run the redfox key setup command")
    client = RedfoxClient(api_key, request_delay=config["settings"]["request_delay"])
    cutoff = time.time() - hours * 3600
    interval_hours = float(config["settings"]["check_hours"])
    discovered: list[dict] = []
    account_errors: list[RedfoxAPIError] = []
    known_links = _queued_links()
    try:
        for subscription in config["subscriptions"]:
            name = str(subscription.get("name", "")).strip()
            alias = str(subscription.get("alias", "")).strip()
            biz = str(subscription.get("biz", "")).strip()
            diagnostic = {
                "account": name or alias,
                "status": "pending",
                "fetched": 0,
                "recent": 0,
                "outside_window": 0,
                "invalid": 0,
                "queued": 0,
                "skipped_cooldown": 0,
            }
            try:
                if not alias:
                    # The wide library identifies accounts by wechat alias
                    # only; neither a display name nor a bare biz id can be
                    # queried.
                    if name or biz:
                        logger.warning(
                            "subscription %s has no wechat alias; the redfox wide "
                            "library cannot query it by display name",
                            name or biz,
                        )
                    diagnostic["status"] = "unresolved"
                    if diagnostics is not None:
                        diagnostics.append(diagnostic)
                    continue
                if not force and _subscription_cooldown_active(
                    subscription, interval_hours, hours
                ):
                    diagnostic["status"] = "ok"
                    diagnostic["skipped_cooldown"] = 1
                    if diagnostics is not None:
                        diagnostics.append(diagnostic)
                    continue
                limit = int(config["settings"]["max_articles_per_account"])
                raw_articles, listing_info = client.list_articles(
                    account=alias,
                    cutoff_epoch=cutoff,
                    max_articles=limit,
                    skip_links=known_links,
                )
                diagnostic["fetched"] = len(raw_articles)
                if listing_info["empty_reason"] == "no_data":
                    # 3203: this library has no such account — most likely a
                    # mistyped alias. Report it instead of hiding behind an
                    # empty "ok" run, and do not arm the paid cooldown.
                    diagnostic["status"] = "unresolved"
                    diagnostic["error"] = "account_not_found"
                    if diagnostics is not None:
                        diagnostics.append(diagnostic)
                    continue
                account_articles: list[dict] = []
                for article in raw_articles:
                    if not article["title"] or not article["link"]:
                        diagnostic["invalid"] += 1
                        continue
                    if article["update_time"] and article["update_time"] < cutoff:
                        diagnostic["outside_window"] += 1
                        continue
                    entry = {
                        "title": article["title"],
                        "link": article["link"],
                        "digest": article["digest"],
                        "account": name or alias,
                        "account_id": alias or biz,
                        "update_time": article["update_time"],
                        "content_source": "redfox",
                    }
                    if article["work_uuid"]:
                        entry["work_uuid"] = article["work_uuid"]
                    account_articles.append(entry)
                    known_links.add(entry["link"])
                    diagnostic["recent"] += 1
                try:
                    if on_account_articles is not None:
                        diagnostic["queued"] = on_account_articles(account_articles)
                except Exception:
                    # Record the failing account before the exception unrolls,
                    # or the partial-run report loses its blocking diagnosis.
                    diagnostic["status"] = "blocked"
                    diagnostic["error"] = "queue_persist_failed"
                    if diagnostics is not None:
                        diagnostics.append(diagnostic)
                    # The paid listing already succeeded; arm the cooldown so a
                    # persistent queue failure cannot re-charge every cycle. If
                    # this write fails too, its error replaces the queue error.
                    _mark_subscription_discovered((name, alias, biz), config_path, hours)
                    raise
                discovered.extend(account_articles)
                diagnostic["status"] = "ok"
                if listing_info["empty_reason"] == "outside_window" and not account_articles:
                    diagnostic["window_empty"] = True
                capped = bool(listing_info.get("more_in_window")) or (
                    listing_info.get("empty_reason") == "limit_reached"
                )
                identity = (name, alias, biz)
                if capped:
                    diagnostic["capped"] = True
                    diagnostic["note"] = "已达每号上限，其余本窗口文章未收录"
                diagnostic["cooldown_armed"] = True
                if diagnostics is not None:
                    diagnostics.append(diagnostic)
                _mark_subscription_discovered(identity, config_path, hours)
            except RedfoxAPIError as exc:
                diagnostic["status"] = "blocked"
                diagnostic["error"] = type(exc).__name__
                if diagnostics is not None:
                    diagnostics.append(diagnostic)
                # A bad key, a rate limit, or a dead network will fail every
                # later account the same way. An account-specific API error
                # must not skip the rest of the roster.
                if exc.retryable or isinstance(exc, RedfoxAuthError):
                    raise
                account_errors.append(exc)
    finally:
        client.close()
    if account_errors:
        raise account_errors[0]
    return discovered



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--hours", type=float)
    parser.add_argument(
        "--force",
        action="store_true",
        help="bypass per-subscription cooldowns (billed list calls are made again)",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    arguments = parser.parse_args(argv)
    json_output = arguments.format == "json"
    diagnostics: list[dict] = []
    queued = 0

    def partial_meta() -> dict:
        completed_accounts = sum(item.get("status") == "ok" for item in diagnostics)
        return {
            "partial": bool(completed_accounts or queued),
            "queued": queued,
            "completed_accounts": completed_accounts,
            "skipped_invalid": sum(int(item.get("invalid", 0)) for item in diagnostics),
            "blocking_account": next(
                (item.get("account", "") for item in diagnostics if item.get("status") == "blocked"),
                "",
            ),
        }

    def report_failure(exc: Exception) -> None:
        if json_output:
            envelope = failure(exc)
            envelope["meta"] = partial_meta()
            print(dump(envelope))
        else:
            meta = partial_meta()
            logger.error(
                "%s (partial=%s, queued=%s, blocking_account=%s)",
                exc,
                meta["partial"],
                meta["queued"],
                meta["blocking_account"],
            )

    try:
        config = load_config(arguments.config)
        if not config["redfox"]["api_key"].strip():
            raise ConfigError("redfox API key is missing; run the redfox key setup command")
        if not config["subscriptions"]:
            raise ConfigError("no subscriptions configured")
        hours = arguments.hours or float(config["settings"]["check_hours"])
        expired_pending = expire_stale_pending(pending_expiry_hours(config))

        def persist_account(articles: list[dict]) -> int:
            nonlocal queued
            added = add_pending(
                articles,
                content_dedup=bool(config["settings"]["content_dedup"]),
            )
            queued += added
            return added

        articles = discover_articles(
            config,
            hours,
            arguments.config,
            diagnostics,
            persist_account,
            force=arguments.force,
        )
        cleanup_processed()
        body_retry = retry_uncrawled_bodies(
            config["redfox"]["api_key"], config["settings"]["request_delay"]
        )
        data = {
            "hours": hours,
            "forced": bool(arguments.force),
            "discovered": len(articles),
            "queued": queued,
            "expired_pending": expired_pending,
            "body_retry": body_retry,
            "accounts": diagnostics,
        }
        if json_output:
            print(dump(success(data, next_action="process_pending_articles")))
        else:
            for item in diagnostics:
                note = f"; {item['note']}" if item.get("note") else ""
                print(
                    f"{item['account']}: {item['status']}; fetched={item['fetched']}; "
                    f"recent={item['recent']}; queued={item['queued']}; "
                    f"invalid={item['invalid']}{note}"
                )
            print(f"Discovered {len(articles)} recent articles; queued {queued} new articles")
        return 0
    except (ConfigError, RedfoxAPIError, ValueError) as exc:
        report_failure(exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
