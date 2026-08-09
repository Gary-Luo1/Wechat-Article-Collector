"""Concurrent-safe local article queue with stable URL identities."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from paths import lock_path, queue_path, secure_write_json
from process_lock import process_lock
from url_identity import normalize_article_url


QUEUE_VERSION = 1
MAX_PROCESSED_AGE_DAYS = 365


def empty_queue() -> dict[str, Any]:
    return {"version": QUEUE_VERSION, "pending": [], "processed": {}}


def normalize_url(url: str) -> str:
    """Normalize a URL into the queue identity key (rule in url_identity)."""
    return normalize_article_url(url)


def content_hash(article: dict[str, Any]) -> str | None:
    """Return a conservative secondary identity or None.

    A title/account pair is not unique: publishers routinely reuse titles and
    omit digests. Only produce a secondary identity when all content signals and
    a publication timestamp are present. Normalized URL remains authoritative.
    """
    values = [
        str(article.get(key, "")).strip()
        for key in ("title", "digest", "account", "update_time")
    ]
    if not all(values) or values[3] in {"0", "None"}:
        return None
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


@contextmanager
def queue_lock(timeout: float = 10.0) -> Iterator[None]:
    """Acquire a cross-platform process lock for queue transactions."""
    with process_lock(lock_path(), timeout=timeout):
        yield


def _validate_queue(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("queue root must be an object")
    pending = data.get("pending")
    processed = data.get("processed")
    if not isinstance(pending, list) or not isinstance(processed, dict):
        raise ValueError("queue must contain pending list and processed object")
    for index, article in enumerate(pending):
        _validate_article(article, f"pending[{index}]")
        normalized = article.get("normalized_url")
        if not isinstance(normalized, str) or not normalized:
            raise ValueError(f"pending[{index}].normalized_url must be a non-empty string")
        if normalize_url(article["link"]) != normalized:
            raise ValueError(f"pending[{index}].normalized_url does not match link")
    for normalized, entry in processed.items():
        if not isinstance(normalized, str) or not normalized:
            raise ValueError("processed keys must be non-empty normalized URLs")
        if not isinstance(entry, dict):
            raise ValueError(f"processed[{normalized!r}] must be an object")
        article = entry.get("article")
        _validate_article(article, f"processed[{normalized!r}].article")
        if normalize_url(article["link"]) != normalized:
            raise ValueError(f"processed[{normalized!r}] key does not match article link")
        if not isinstance(entry.get("metadata"), dict):
            raise ValueError(f"processed[{normalized!r}].metadata must be an object")
        if not isinstance(entry.get("processed_at"), str):
            raise ValueError(f"processed[{normalized!r}].processed_at must be a string")
        if not isinstance(entry.get("sync_status"), str):
            raise ValueError(f"processed[{normalized!r}].sync_status must be a string")
    return {"version": QUEUE_VERSION, "pending": pending, "processed": processed}


def _validate_article(article: Any, location: str) -> None:
    if not isinstance(article, dict):
        raise ValueError(f"{location} must be an object")
    link = article.get("link")
    if not isinstance(link, str) or not link.strip():
        raise ValueError(f"{location}.link must be a non-empty string")
    normalize_url(link)
    for key in ("title", "digest", "account"):
        if key in article and not isinstance(article[key], str):
            raise ValueError(f"{location}.{key} must be a string")
    for key in ("id", "normalized_url", "content_hash", "discovered_at", "inbox_updated_at"):
        if key in article and article[key] is not None and not isinstance(article[key], str):
            raise ValueError(f"{location}.{key} must be a string")
    if "favorite" in article and not isinstance(article["favorite"], bool):
        raise ValueError(f"{location}.favorite must be boolean")
    if article.get("inbox_state", "active") not in {"active", "later"}:
        raise ValueError(f"{location}.inbox_state must be active or later")
    read_state = article.get("read_state")
    if read_state is not None:
        if not isinstance(read_state, dict):
            raise ValueError(f"{location}.read_state must be an object")
        if read_state.get("status") != "verified":
            raise ValueError(f"{location}.read_state.status must be verified")
        if not isinstance(read_state.get("verified_at"), str):
            raise ValueError(f"{location}.read_state.verified_at must be a string")
        fingerprint = read_state.get("content_sha256")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError(f"{location}.read_state.content_sha256 must be a SHA-256 hex digest")


def _read_unlocked() -> dict[str, Any]:
    path = queue_path()
    if not path.exists():
        return empty_queue()
    try:
        return _validate_queue(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = path.with_name(f"queue.corrupt.{timestamp}.json")
        try:
            os.replace(path, quarantine)
        except OSError:
            pass
        raise ValueError(f"queue is invalid; preserved as {quarantine}: {exc}") from exc


def _write_unlocked(data: dict[str, Any]) -> None:
    secure_write_json(queue_path(), _validate_queue(data))


def read_queue() -> dict[str, Any]:
    with queue_lock():
        return deepcopy(_read_unlocked())


def get_pending() -> list[dict[str, Any]]:
    return read_queue()["pending"]


def add_pending(articles: list[dict[str, Any]], *, content_dedup: bool = False) -> int:
    """Add articles in one locked transaction and apply normalized URL dedup."""
    with queue_lock():
        data = _read_unlocked()
        existing_urls = set(data["processed"])
        existing_urls.update(item["normalized_url"] for item in data["pending"])
        hashes = {
            item.get("content_hash")
            for item in data["pending"]
            if item.get("content_hash")
        }
        hashes.update(
            entry.get("content_hash")
            for entry in data["processed"].values()
            if isinstance(entry, dict) and entry.get("content_hash")
        )
        added = 0
        for source in articles:
            article = deepcopy(source)
            normalized = normalize_url(str(article.get("link", "")))
            digest = content_hash(article)
            if normalized in existing_urls or (content_dedup and digest in hashes):
                continue
            article["id"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
            article["normalized_url"] = normalized
            if digest:
                article["content_hash"] = digest
            article["discovered_at"] = datetime.now(timezone.utc).isoformat()
            article.setdefault("favorite", False)
            article.setdefault("inbox_state", "active")
            article["inbox_updated_at"] = article["discovered_at"]
            data["pending"].append(article)
            existing_urls.add(normalized)
            if digest:
                hashes.add(digest)
            added += 1
        _write_unlocked(data)
        return added


def resolve_pending(*, index: int | None = None, link: str | None = None) -> dict[str, Any]:
    pending = get_pending()
    if link:
        normalized = normalize_url(link)
        for article in pending:
            if article.get("normalized_url") == normalized:
                return article
        raise LookupError("no pending article matches that URL")
    if index is None or index < 0 or index >= len(pending):
        raise LookupError(f"article index must be between 1 and {len(pending)}")
    return pending[index]


def has_verified_read(article: dict[str, Any]) -> bool:
    """Return whether an article carries a validated full-text read proof."""
    state = article.get("read_state")
    return (
        isinstance(state, dict)
        and state.get("status") == "verified"
        and isinstance(state.get("verified_at"), str)
        and isinstance(state.get("content_sha256"), str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", state["content_sha256"]))
    )


def record_verified_read(link: str, text: str) -> dict[str, Any]:
    """Atomically persist bounded proof that a pending article was read."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("article text must be non-empty before recording a verified read")
    normalized = normalize_url(link)
    state = {
        "status": "verified",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    with queue_lock():
        data = _read_unlocked()
        article = next(
            (item for item in data["pending"] if item.get("normalized_url") == normalized),
            None,
        )
        if article is None:
            raise LookupError("article is no longer pending")
        article["read_state"] = state
        _write_unlocked(data)
        return deepcopy(article)


def update_inbox_item(
    link: str,
    *,
    favorite: bool | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Update reversible inbox metadata on a pending or processed article."""
    if favorite is None and state is None:
        raise ValueError("provide favorite or state update")
    if state is not None and state not in {"active", "later"}:
        raise ValueError("inbox state must be active or later")
    normalized = normalize_url(link)
    with queue_lock():
        data = _read_unlocked()
        article = next(
            (item for item in data["pending"] if item.get("normalized_url") == normalized),
            None,
        )
        location = "pending"
        if article is None:
            entry = data["processed"].get(normalized)
            article = entry.get("article") if isinstance(entry, dict) else None
            location = "processed"
        if not isinstance(article, dict):
            raise LookupError("article not found in inbox")
        if state is not None and location != "pending":
            raise ValueError("only pending articles can be moved to active or later")
        if favorite is not None:
            article["favorite"] = favorite
        if state is not None:
            article["inbox_state"] = state
        article["inbox_updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_unlocked(data)
        return {
            "location": location,
            "article": deepcopy(article),
            "favorite": bool(article.get("favorite", False)),
            "inbox_state": str(article.get("inbox_state", "active")),
        }


def dismiss_article(link: str) -> dict[str, Any]:
    """Move a pending article to a reversible dismissed processed entry."""
    normalized = normalize_url(link)
    with queue_lock():
        data = _read_unlocked()
        article = next(
            (item for item in data["pending"] if item.get("normalized_url") == normalized),
            None,
        )
        if article is None:
            existing = data["processed"].get(normalized)
            if (
                isinstance(existing, dict)
                and existing.get("metadata", {}).get("disposition") == "dismissed"
            ):
                return deepcopy(existing)
            raise LookupError("only pending articles can be dismissed")
        data["pending"] = [
            item for item in data["pending"] if item.get("normalized_url") != normalized
        ]
        now = datetime.now(timezone.utc).isoformat()
        article["inbox_updated_at"] = now
        entry = {
            "article": deepcopy(article),
            "content_hash": article.get("content_hash"),
            "processed_at": now,
            "sync_status": "not_requested",
            "metadata": {"disposition": "dismissed"},
        }
        data["processed"][normalized] = entry
        _write_unlocked(data)
        return deepcopy(entry)


def restore_dismissed(link: str) -> dict[str, Any]:
    """Restore one dismissed entry to pending without changing its URL identity."""
    normalized = normalize_url(link)
    with queue_lock():
        data = _read_unlocked()
        entry = data["processed"].get(normalized)
        if not isinstance(entry, dict) or entry.get("metadata", {}).get("disposition") != "dismissed":
            raise LookupError("dismissed article not found")
        if any(item.get("normalized_url") == normalized for item in data["pending"]):
            raise ValueError("article is already pending")
        article = deepcopy(entry["article"])
        article["inbox_state"] = "active"
        article["inbox_updated_at"] = datetime.now(timezone.utc).isoformat()
        data["pending"].append(article)
        del data["processed"][normalized]
        _write_unlocked(data)
        return deepcopy(article)


def complete_article(
    link: str,
    metadata: dict[str, Any] | None = None,
    *,
    sync_status: str = "not_requested",
) -> dict[str, Any]:
    """Move one article to processed using its stable normalized URL."""
    normalized = normalize_url(link)
    with queue_lock():
        data = _read_unlocked()
        article = next(
            (item for item in data["pending"] if item.get("normalized_url") == normalized),
            None,
        )
        if article is None:
            existing = data["processed"].get(normalized)
            if existing:
                if existing.get("metadata", {}).get("disposition") == "dismissed":
                    raise LookupError(
                        "article was dismissed and cannot be completed; restore it first"
                    )
                return deepcopy(existing)
            raise LookupError("article is no longer pending")
        data["pending"] = [
            item for item in data["pending"] if item.get("normalized_url") != normalized
        ]
        entry = {
            "article": deepcopy(article),
            "content_hash": article.get("content_hash"),
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "sync_status": sync_status,
            "metadata": deepcopy(metadata or {}),
        }
        data["processed"][normalized] = entry
        _write_unlocked(data)
        return deepcopy(entry)


def update_sync_status(link: str, status: str, error: str = "") -> None:
    normalized = normalize_url(link)
    with queue_lock():
        data = _read_unlocked()
        entry = data["processed"].get(normalized)
        if not entry:
            raise LookupError("processed article not found")
        entry["sync_status"] = status
        entry["sync_updated_at"] = datetime.now(timezone.utc).isoformat()
        if error:
            entry["sync_error"] = error[:500]
        else:
            entry.pop("sync_error", None)
        _write_unlocked(data)


def pending_sync_entries() -> list[dict[str, Any]]:
    data = read_queue()
    return [
        deepcopy(entry)
        for entry in data["processed"].values()
        if isinstance(entry, dict) and entry.get("sync_status") == "pending"
    ]


def cleanup_processed(max_age_days: int = MAX_PROCESSED_AGE_DAYS) -> int:
    if max_age_days < 1:
        raise ValueError("max_age_days must be positive")
    cutoff = time.time() - max_age_days * 86400
    with queue_lock():
        data = _read_unlocked()
        before = len(data["processed"])
        retained = {}
        for link, entry in data["processed"].items():
            if entry.get("sync_status") == "pending":
                retained[link] = entry
                continue
            try:
                timestamp = datetime.fromisoformat(entry["processed_at"]).timestamp()
            except (KeyError, TypeError, ValueError):
                retained[link] = entry
                continue
            if timestamp >= cutoff:
                retained[link] = entry
        data["processed"] = retained
        _write_unlocked(data)
        return before - len(retained)


def export_queue(path: Path) -> Path:
    secure_write_json(Path(path), read_queue())
    return Path(path)
