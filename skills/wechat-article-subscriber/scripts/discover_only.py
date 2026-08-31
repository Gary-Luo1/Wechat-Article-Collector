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
from queue_helpers import add_pending, cleanup_processed
from redfox_client import RedfoxAPIError, RedfoxClient


logger = logging.getLogger("wechat-discover")


def _subscription_cooldown_active(subscription: dict, interval_hours: float) -> bool:
    """Paid-API guard: skip a subscription still inside its discovery cooldown."""
    raw = str(subscription.get("last_discovered_at", "")).strip()
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() < interval_hours * 3600


def _mark_subscription_discovered(identity: tuple[str, str, str], config_path: Path | None) -> None:
    now = datetime.now(timezone.utc).isoformat()

    def mutate(saved: dict) -> dict:
        for sub in saved["subscriptions"]:
            if (
                str(sub.get("name", "")).strip(),
                str(sub.get("alias", "")).strip(),
                str(sub.get("biz", "")).strip(),
            ) == identity:
                sub["last_discovered_at"] = now
                return saved
        # A concurrent edit changed the subscription identity; without the
        # timestamp the paid-call cooldown cannot apply next cycle.
        logger.warning(
            "subscription %s changed during discovery; cooldown timestamp not saved",
            "/".join(part or "-" for part in identity),
        )
        return saved

    modify_config(mutate, path=config_path)


def discover_articles(
    config: dict,
    hours: float,
    config_path: Path | None = None,
    diagnostics: list[dict] | None = None,
    on_account_articles: Callable[[list[dict]], int] | None = None,
) -> list[dict]:
    """Discover articles through the paid redfox API (billing-aware)."""
    api_key = config["redfox"]["api_key"].strip()
    if not api_key:
        raise ConfigError("redfox API key is missing; run the redfox key setup command")
    client = RedfoxClient(api_key, request_delay=config["settings"]["request_delay"])
    cutoff = time.time() - hours * 3600
    interval_hours = float(config["settings"]["check_hours"])
    discovered: list[dict] = []
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
                if _subscription_cooldown_active(subscription, interval_hours):
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
                    diagnostic["recent"] += 1
                try:
                    if on_account_articles is not None:
                        diagnostic["queued"] = on_account_articles(account_articles)
                except Exception:
                    # The paid listing already succeeded; arm the cooldown so a
                    # persistent queue failure cannot re-charge every cycle.
                    _mark_subscription_discovered((name, alias, biz), config_path)
                    raise
                discovered.extend(account_articles)
                diagnostic["status"] = "ok"
                if listing_info["empty_reason"] == "outside_window" and not account_articles:
                    diagnostic["window_empty"] = True
                if diagnostics is not None:
                    diagnostics.append(diagnostic)
                _mark_subscription_discovered((name, alias, biz), config_path)
            except RedfoxAPIError as exc:
                diagnostic["status"] = "blocked"
                diagnostic["error"] = type(exc).__name__
                if diagnostics is not None:
                    diagnostics.append(diagnostic)
                raise
    finally:
        client.close()
    return discovered



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--hours", type=float)
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
        )
        cleanup_processed()
        data = {
            "hours": hours,
            "discovered": len(articles),
            "queued": queued,
            "accounts": diagnostics,
        }
        if json_output:
            print(dump(success(data, next_action="process_pending_articles")))
        else:
            for item in diagnostics:
                print(
                    f"{item['account']}: {item['status']}; fetched={item['fetched']}; "
                    f"recent={item['recent']}; queued={item['queued']}; "
                    f"invalid={item['invalid']}"
                )
            print(f"Discovered {len(articles)} recent articles; queued {queued} new articles")
        return 0
    except (ConfigError, RedfoxAPIError, ValueError) as exc:
        report_failure(exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
