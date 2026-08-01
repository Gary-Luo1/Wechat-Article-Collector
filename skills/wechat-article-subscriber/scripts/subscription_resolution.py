"""Shared publisher identity and subscription-resolution rules."""

from __future__ import annotations

from typing import Any


def normalize_publisher(value: Any) -> str:
    """Normalize human-entered publisher names for an identity comparison."""
    return " ".join(str(value or "").split()).casefold()


def subscription_query(subscription: dict[str, Any]) -> str:
    """Choose the stable, user-facing search query for one subscription."""
    alias = str(subscription.get("alias", "")).strip()
    return alias or str(subscription.get("name", "")).strip()


def matches_subscription(
    subscriptions: list[dict[str, Any]], account: str, account_id: str
) -> bool:
    """Match an observed publisher against configured names, aliases, or biz ID."""
    account_key = normalize_publisher(account)
    account_id_key = str(account_id or "").strip().casefold()
    for subscription in subscriptions:
        names = {
            normalize_publisher(subscription.get(key))
            for key in ("name", "alias")
            if normalize_publisher(subscription.get(key))
        }
        biz = str(subscription.get("biz", "")).strip().casefold()
        if account_key in names or (account_id_key and biz == account_id_key):
            return True
    return False


def sanitize_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Project search responses to the subscription fields persisted by this skill."""
    return [
        {
            "name": str(item.get("nickname", "")),
            "alias": str(item.get("alias", "")),
            "biz": str(item.get("fakeid", "")),
        }
        for item in candidates
        if isinstance(item, dict)
    ]


def exact_matches(
    subscription: dict[str, Any], candidates: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Return all normalized exact publisher matches; never choose among multiples."""
    name = normalize_publisher(subscription.get("name"))
    alias = normalize_publisher(subscription.get("alias"))
    return [
        item
        for item in candidates
        if (alias and normalize_publisher(item["alias"]) == alias)
        or (name and normalize_publisher(item["name"]) == name)
    ]
