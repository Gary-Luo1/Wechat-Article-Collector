#!/usr/bin/env python3
"""Discover recent articles and append them to the local queue."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from pathlib import Path

from config_store import ConfigError, load_config, modify_config, update_health
from protocol import dump, failure, success
from queue_helpers import add_pending, cleanup_processed
from subscription_resolution import exact_matches, sanitize_candidates, subscription_query
from url_identity import canonicalize_wechat_article_url
from wechat_api import (
    WeChatAPI,
    WeChatAccessRestricted,
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
    unresolved = 0
    pending: list[tuple[tuple[str, str, str], tuple[str, str, str]]] = []
    for subscription in config["subscriptions"]:
        name = str(subscription.get("name", "")).strip()
        alias = str(subscription.get("alias", "")).strip()
        biz = str(subscription.get("biz", "")).strip()
        query = subscription_query(subscription)
        if biz:
            results.append(
                {"query": query, "status": "resolved", "name": name, "alias": alias, "biz": biz}
            )
            continue
        candidates = client.search_account(query, count=5)
        sanitized = sanitize_candidates(candidates)
        exact = exact_matches(subscription, sanitized)
        if len(exact) == 1 and exact[0]["biz"]:
            status = "exact"
            if save:
                pending.append(
                    (
                        (
                            str(subscription.get("name", "")).strip(),
                            str(subscription.get("alias", "")).strip(),
                            str(subscription.get("biz", "")).strip(),
                        ),
                        (
                            exact[0]["biz"],
                            str(subscription.get("name", "")).strip()
                            or str(exact[0].get("name", "")).strip(),
                            str(subscription.get("alias", "")).strip()
                            or str(exact[0].get("alias", "")).strip(),
                        ),
                    )
                )
        elif len(exact) > 1:
            status = "ambiguous"
            unresolved += 1
        else:
            status = "not_found"
            unresolved += 1
        results.append(
            {"query": query, "status": status, "exact": exact, "candidates": sanitized}
        )
    if save and pending:
        def mutate(config: dict) -> dict:
            for original, resolved in pending:
                for sub in config["subscriptions"]:
                    if (
                        str(sub.get("name", "")).strip(),
                        str(sub.get("alias", "")).strip(),
                        str(sub.get("biz", "")).strip(),
                    ) == original:
                        sub["biz"] = resolved[0]
                        if not str(sub.get("name", "")).strip():
                            sub["name"] = resolved[1]
                        if not str(sub.get("alias", "")).strip():
                            sub["alias"] = resolved[2]
                        break
            return config

        modify_config(mutate, path=config_path)
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
    on_account_articles: Callable[[list[dict]], int] | None = None,
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
        }
        try:
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
                original = (name, alias, "")

                def mutate(saved: dict) -> dict:
                    for sub in saved["subscriptions"]:
                        if (
                            str(sub.get("name", "")).strip(),
                            str(sub.get("alias", "")).strip(),
                            str(sub.get("biz", "")).strip(),
                        ) == original:
                            sub["biz"] = biz
                            break
                    return saved

                modify_config(mutate, path=config_path)
                subscription["biz"] = biz
            limit = int(settings["max_articles_per_account"])
            begin = 0
            articles: list[dict] = []
            while len(articles) < limit:
                batch, _ = api.list_articles(
                    biz, begin=begin, count=min(5, limit - len(articles))
                )
                articles.extend(batch)
                if len(batch) < 5 or not batch:
                    break
                if int(batch[-1].get("update_time", 0) or 0) < cutoff:
                    break
                begin += len(batch)
            diagnostic["fetched"] = len(articles[:limit])
            account_articles: list[dict] = []
            for raw in articles[:limit]:
                article = api.format_article(raw)
                if not article["title"] or not article["link"]:
                    diagnostic["invalid"] += 1
                    continue
                try:
                    link = canonicalize_wechat_article_url(article["link"])
                except ValueError:
                    diagnostic["invalid"] += 1
                    continue
                if article["update_time"] < cutoff:
                    diagnostic["outside_window"] += 1
                    continue
                account_articles.append(
                    {
                        "title": article["title"],
                        "link": link,
                        "digest": article["digest"],
                        "account": name or alias,
                        "account_id": alias or biz,
                        "update_time": article["update_time"],
                    }
                )
                diagnostic["recent"] += 1
            if on_account_articles is not None:
                diagnostic["queued"] = on_account_articles(account_articles)
            discovered.extend(account_articles)
            diagnostic["status"] = "ok"
            if diagnostics is not None:
                diagnostics.append(diagnostic)
        except (WeChatTokenExpired, WeChatCookieExpired, WeChatCredentialContextError,
                WeChatAccessRestricted,
                WeChatRateLimitError, WeChatAPIError) as exc:
            diagnostic["status"] = "blocked"
            diagnostic["error"] = type(exc).__name__
            if diagnostics is not None:
                diagnostics.append(diagnostic)
            raise
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
        config = load_config(arguments.config, require_wechat=True)
        if arguments.check_token:
            api = WeChatAPI(
                config["wechat"]["cookie"],
                config["wechat"]["token"],
                request_delay=config["settings"]["request_delay"],
            )
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
        try:
            update_health("wechat", success=True, path=arguments.config)
        except ConfigError:
            pass
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
    except (WeChatTokenExpired, WeChatCookieExpired, WeChatAccessRestricted) as exc:
        if config is not None:
            try:
                update_health("wechat", success=False, failure_kind=type(exc).__name__, path=arguments.config)
            except ConfigError:
                pass
        report_failure(exc)
        return 2
    except WeChatCredentialContextError as exc:
        if config is not None:
            try:
                update_health("wechat", success=False, failure_kind=type(exc).__name__, path=arguments.config)
            except ConfigError:
                pass
        report_failure(exc)
        return 3
    except (ConfigError, WeChatRateLimitError, WeChatAPIError, ValueError) as exc:
        report_failure(exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
