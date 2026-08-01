#!/usr/bin/env python3
"""Discover recent articles and append them to the local queue."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from config_store import ConfigError, load_config, save_config, update_health
from protocol import dump, failure, success
from queue_helpers import add_pending, cleanup_processed, normalize_url, read_queue
from wechat_api import (
    WeChatAPI,
    WeChatAPIError,
    WeChatCookieExpired,
    WeChatCredentialContextError,
    WeChatRateLimitError,
    WeChatTokenExpired,
)


logger = logging.getLogger("wechat-discover")


def resolve_subscriptions(
    config: dict,
    *,
    api: WeChatAPI | None = None,
    save: bool = False,
    config_path: Path | None = None,
) -> list[dict]:
    client = api or WeChatAPI(
        config["wechat"]["cookie"],
        config["wechat"]["token"],
        request_delay=config["settings"]["request_delay"],
    )
    results: list[dict] = []
    changed = False
    unresolved = 0
    for subscription in config["subscriptions"]:
        name = str(subscription.get("name", "")).strip()
        alias = str(subscription.get("alias", "")).strip()
        biz = str(subscription.get("biz", "")).strip()
        query = alias or name
        if biz:
            results.append(
                {"query": query, "status": "resolved", "name": name, "alias": alias, "biz": biz}
            )
            continue
        candidates = client.search_account(query, count=5)
        sanitized = [
            {
                "name": str(item.get("nickname", "")),
                "alias": str(item.get("alias", "")),
                "biz": str(item.get("fakeid", "")),
            }
            for item in candidates
            if isinstance(item, dict)
        ]
        exact = [
            item
            for item in sanitized
            if (alias and item["alias"] == alias) or (name and item["name"] == name)
        ]
        if len(exact) == 1 and exact[0]["biz"]:
            status = "exact"
            if save:
                subscription["biz"] = exact[0]["biz"]
                if not subscription.get("name"):
                    subscription["name"] = exact[0]["name"]
                if not subscription.get("alias"):
                    subscription["alias"] = exact[0]["alias"]
                changed = True
        elif len(exact) > 1:
            status = "ambiguous"
            unresolved += 1
        else:
            status = "not_found"
            unresolved += 1
        results.append(
            {"query": query, "status": status, "exact": exact, "candidates": sanitized}
        )
    if save and changed:
        save_config(config, config_path)
    try:
        update_health(
            "subscriptions",
            success=unresolved == 0,
            unresolved=unresolved,
            path=config_path,
        )
    except ConfigError:
        pass
    return results


def discover_articles(
    config: dict,
    hours: float,
    config_path: Path | None = None,
    diagnostics: list[dict] | None = None,
) -> list[dict]:
    wechat = config["wechat"]
    settings = config["settings"]
    api = WeChatAPI(
        wechat["cookie"],
        wechat["token"],
        request_delay=settings["request_delay"],
    )
    cutoff = time.time() - hours * 3600
    discovered: list[dict] = []
    config_changed = False
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
        }
        if not biz:
            account = api.get_account(name=name, alias=alias)
            if not account:
                logger.warning("No exact account match for %s; skipping", alias or name)
                diagnostic["status"] = "unresolved"
                if diagnostics is not None:
                    diagnostics.append(diagnostic)
                continue
            biz = str(account.get("fakeid", ""))
            if not biz:
                logger.warning("Account %s has no fakeid; skipping", alias or name)
                diagnostic["status"] = "missing_biz"
                if diagnostics is not None:
                    diagnostics.append(diagnostic)
                continue
            subscription["biz"] = biz
            config_changed = True
        limit = int(settings["max_articles_per_account"])
        begin = 0
        articles: list[dict] = []
        while len(articles) < limit:
            batch, _ = api.list_articles(biz, begin=begin, count=min(5, limit - len(articles)))
            articles.extend(batch)
            if len(batch) < 5 or not batch:
                break
            if int(batch[-1].get("update_time", 0) or 0) < cutoff:
                break
            begin += len(batch)
        diagnostic["fetched"] = len(articles[:limit])
        for raw in articles[:limit]:
            article = api.format_article(raw)
            if not article["title"] or not article["link"]:
                continue
            if article["update_time"] < cutoff:
                diagnostic["outside_window"] += 1
                continue
            discovered.append(
                {
                    "title": article["title"],
                    "link": article["link"],
                    "digest": article["digest"],
                    "account": name or alias,
                    "account_id": alias or biz,
                    "update_time": article["update_time"],
                }
            )
            diagnostic["recent"] += 1
        diagnostic["status"] = "ok"
        if diagnostics is not None:
            diagnostics.append(diagnostic)
    if config_changed:
        save_config(config, config_path)
    return discovered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--check-token", action="store_true")
    parser.add_argument("--hours", type=float)
    parser.add_argument("--resolve-subscriptions", action="store_true")
    parser.add_argument("--save-resolved", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    arguments = parser.parse_args(argv)
    json_output = arguments.format == "json"
    config = None
    try:
        config = load_config(arguments.config, require_wechat=True)
        if arguments.check_token:
            api = WeChatAPI(config["wechat"]["cookie"], config["wechat"]["token"])
            api.search_account("微信", count=1)
            config = update_health("wechat", success=True, path=arguments.config)
            data = {"credentials": "valid", "last_verified": config["health"]["wechat"]}
            print(dump(success(data, next_action="verify_subscriptions")) if json_output else "WeChat credentials are valid")
            return 0
        if arguments.resolve_subscriptions:
            results = resolve_subscriptions(
                config, save=arguments.save_resolved, config_path=arguments.config
            )
            unresolved = sum(item["status"] in {"ambiguous", "not_found"} for item in results)
            data = {"subscriptions": results, "unresolved": unresolved, "saved": arguments.save_resolved}
            if json_output:
                print(
                    dump(
                        success(
                            data,
                            next_action=(
                                "ask_user_to_disambiguate" if unresolved else "run_discovery"
                            ),
                        )
                    )
                )
            else:
                for item in results:
                    print(f"{item['query']}: {item['status']}")
            return 0 if unresolved == 0 else 4
        if arguments.save_resolved:
            raise ValueError("--save-resolved requires --resolve-subscriptions")
        if not config["subscriptions"]:
            raise ConfigError("no subscriptions configured")
        hours = arguments.hours or float(config["settings"]["check_hours"])
        diagnostics: list[dict] = []
        articles = discover_articles(config, hours, arguments.config, diagnostics)
        queue_snapshot = read_queue()
        existing_urls = {
            item["normalized_url"] for item in queue_snapshot["pending"]
        } | set(queue_snapshot["processed"])
        for diagnostic in diagnostics:
            account = diagnostic["account"]
            diagnostic["new_candidates"] = sum(
                item.get("account") == account
                and normalize_url(item["link"]) not in existing_urls
                for item in articles
            )
        added = add_pending(
            articles,
            content_dedup=bool(config["settings"]["content_dedup"]),
        )
        cleanup_processed()
        try:
            update_health("wechat", success=True, path=arguments.config)
        except ConfigError:
            pass
        data = {
            "hours": hours,
            "discovered": len(articles),
            "queued": added,
            "accounts": diagnostics,
        }
        if json_output:
            print(dump(success(data, next_action="process_pending_articles")))
        else:
            for item in diagnostics:
                print(
                    f"{item['account']}: {item['status']}; fetched={item['fetched']}; "
                    f"recent={item['recent']}; new={item.get('new_candidates', 0)}"
                )
            print(f"Discovered {len(articles)} recent articles; queued {added} new articles")
        return 0
    except (WeChatTokenExpired, WeChatCookieExpired) as exc:
        if config is not None:
            try:
                update_health("wechat", success=False, failure_kind=type(exc).__name__, path=arguments.config)
            except ConfigError:
                pass
        print(dump(failure(exc))) if json_output else logger.error("%s", exc)
        return 2
    except WeChatCredentialContextError as exc:
        if config is not None:
            try:
                update_health("wechat", success=False, failure_kind=type(exc).__name__, path=arguments.config)
            except ConfigError:
                pass
        print(dump(failure(exc))) if json_output else logger.error("%s", exc)
        return 3
    except (ConfigError, WeChatRateLimitError, WeChatAPIError, ValueError) as exc:
        print(dump(failure(exc))) if json_output else logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
