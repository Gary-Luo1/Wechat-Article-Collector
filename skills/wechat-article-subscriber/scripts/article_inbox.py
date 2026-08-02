"""Article inbox queries and digest planning over the local queue."""

from __future__ import annotations

from datetime import datetime
import time
from typing import Any

from queue_helpers import get_pending, read_queue


def _timestamp(item: dict[str, Any]) -> float:
    article = item["article"]
    try:
        published = float(article.get("update_time") or 0)
    except (TypeError, ValueError):
        published = 0
    if published:
        return published
    for key in ("processed_at", "discovered_at"):
        value = item.get(key) or article.get(key)
        if value:
            try:
                return datetime.fromisoformat(str(value)).timestamp()
            except ValueError:
                continue
    return 0


def _item_matches(
    item: dict[str, Any],
    *,
    account: str,
    query: str,
    favorite: bool,
    state: str,
    disposition: str,
) -> bool:
    article = item["article"]
    if favorite and not item["favorite"]:
        return False
    if item["status"] == "pending" and state != "all" and item["inbox_state"] != state:
        return False
    if (
        item["status"] == "processed"
        and disposition != "all"
        and item["disposition"] != disposition
    ):
        return False
    if account and str(article.get("account", "")).strip().casefold() != account:
        return False
    searchable = " ".join(
        [
            str(article.get("title", "")),
            str(article.get("account", "")),
            str(article.get("digest", "")),
            str(item.get("summary", "")),
            " ".join(str(tag) for tag in item.get("tags", [])),
        ]
    ).casefold()
    return not query or query in searchable


def _queue_summary(queue: dict[str, Any]) -> dict[str, Any]:
    """One summary implementation shared by the inbox view and callers."""
    return {
        "pending": len(queue["pending"]),
        "processed": len(queue["processed"]),
        "favorites": sum(
            bool(article.get("favorite", False)) for article in queue["pending"]
        )
        + sum(
            bool(entry.get("article", {}).get("favorite", False))
            for entry in queue["processed"].values()
            if isinstance(entry, dict)
        ),
        "later": sum(
            article.get("inbox_state", "active") == "later"
            for article in queue["pending"]
        ),
        "dismissed": sum(
            entry.get("metadata", {}).get("disposition") == "dismissed"
            for entry in queue["processed"].values()
            if isinstance(entry, dict)
        ),
        "sync_pending": sum(
            entry.get("sync_status") == "pending"
            for entry in queue["processed"].values()
            if isinstance(entry, dict)
        ),
    }


def queue_summary() -> dict[str, Any]:
    """Return one consistent summary of pending and processed articles."""
    return _queue_summary(read_queue())


def known_urls() -> set[str]:
    """Return every article URL identity currently in the queue."""
    queue = read_queue()
    return {item["normalized_url"] for item in queue["pending"]} | set(
        queue["processed"]
    )


def query_inbox(
    *,
    status: str = "pending",
    account: str = "",
    query: str = "",
    sort: str = "newest",
    limit: int = 20,
    favorite: bool = False,
    state: str = "all",
    disposition: str = "all",
) -> dict[str, Any]:
    """Return one stable, filtered view of pending and processed articles."""
    if status not in {"pending", "processed", "all"}:
        raise ValueError("status must be pending, processed, or all")
    if sort not in {"newest", "oldest"}:
        raise ValueError("sort must be newest or oldest")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if state not in {"active", "later", "all"}:
        raise ValueError("state must be active, later, or all")
    if disposition not in {"completed", "dismissed", "all"}:
        raise ValueError("disposition must be completed, dismissed, or all")

    queue = read_queue()
    items: list[dict[str, Any]] = []
    if status in {"pending", "all"}:
        items.extend(
            {
                "status": "pending",
                "pending_index": index,
                "article": article,
                "discovered_at": article.get("discovered_at", ""),
                "favorite": bool(article.get("favorite", False)),
                "inbox_state": str(article.get("inbox_state", "active")),
            }
            for index, article in enumerate(queue["pending"], start=1)
        )
    if status in {"processed", "all"}:
        items.extend(
            {
                "status": "processed",
                "article": entry["article"],
                "processed_at": entry.get("processed_at", ""),
                "sync_status": entry.get("sync_status", ""),
                "score": entry.get("metadata", {}).get("score"),
                "summary": entry.get("metadata", {}).get("summary", ""),
                "tags": entry.get("metadata", {}).get("tags", []),
                "favorite": bool(entry["article"].get("favorite", False)),
                "inbox_state": str(entry["article"].get("inbox_state", "active")),
                "disposition": str(entry.get("metadata", {}).get("disposition", "completed")),
            }
            for entry in queue["processed"].values()
            if isinstance(entry, dict) and isinstance(entry.get("article"), dict)
        )

    normalized_account = " ".join(account.split()).casefold()
    normalized_query = " ".join(query.split()).casefold()
    selected = [
        item
        for item in items
        if _item_matches(
            item,
            account=normalized_account,
            query=normalized_query,
            favorite=favorite,
            state=state,
            disposition=disposition,
        )
    ]
    selected.sort(key=_timestamp, reverse=sort == "newest")
    matched = len(selected)
    selected = selected[:limit]
    return {
        "summary": {**_queue_summary(queue), "matched": matched, "returned": len(selected)},
        "filters": {
            "status": status,
            "account": account,
            "query": query,
            "sort": sort,
            "limit": limit,
            "favorite": bool(favorite),
            "state": state,
            "disposition": disposition,
        },
        "items": selected,
    }


def plan_digest(
    preferences: dict[str, Any],
    *,
    hours: int | float,
    limit: int,
    include_later: bool = False,
) -> dict[str, Any]:
    """Select pending Article inbox entries without reading or completing them."""
    if not 1 <= hours <= 8760:
        raise ValueError("hours must be between 1 and 8760")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    cutoff = time.time() - hours * 3600
    include_topics = [str(value).casefold() for value in preferences["include_topics"]]
    exclude_keywords = [
        str(value).casefold() for value in preferences["exclude_keywords"]
    ]
    preferred_accounts = {
        str(value).casefold() for value in preferences["preferred_accounts"]
    }
    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    excluded = {"too_old": 0, "later": 0, "keyword": 0}
    for article in get_pending():
        state = str(article.get("inbox_state", "active"))
        if state == "later" and not include_later:
            excluded["later"] += 1
            continue
        timestamp = _timestamp(
            {"article": article, "discovered_at": article.get("discovered_at", "")}
        )
        if timestamp and timestamp < cutoff:
            excluded["too_old"] += 1
            continue
        searchable = " ".join(
            str(article.get(key, "")) for key in ("title", "digest", "account")
        ).casefold()
        if any(keyword in searchable for keyword in exclude_keywords):
            excluded["keyword"] += 1
            continue
        topic_matches = [topic for topic in include_topics if topic in searchable]
        account = str(article.get("account", "")).strip()
        preferred_account = account.casefold() in preferred_accounts
        favorite = bool(article.get("favorite", False))
        reasons = []
        if favorite:
            reasons.append("favorite")
        if preferred_account:
            reasons.append("preferred_account")
        if topic_matches:
            reasons.append("topic_match")
        candidates.append(
            (
                (favorite, preferred_account, len(topic_matches), timestamp),
                {
                    "title": str(article.get("title", "")),
                    "account": account,
                    "link": str(article.get("link", "")),
                    "url": str(article.get("link", "")),
                    "published_at": article.get("update_time", 0),
                    "favorite": favorite,
                    "inbox_state": state,
                    "matched_topics": topic_matches,
                    "selection_reasons": reasons or ["recent"],
                },
            )
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = [item for _, item in candidates[:limit]]
    return {
        "window_hours": hours,
        "limit": limit,
        "include_later": bool(include_later),
        "preferences": preferences,
        "eligible": len(candidates),
        "returned": len(selected),
        "excluded": excluded,
        "candidates": selected,
        "content_fetched": False,
        "articles_completed": False,
        "feishu_written": False,
    }
