"""Client for the redfox.hk paid WeChat Official Account data API.

Every call to this service costs money, so the client is deliberately
conservative: no implicit retries beyond transient transport failures, no
speculative pagination, and one request per account-listing page.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from http_client import is_transient_network_error, new_session
from url_identity import canonicalize_wechat_article_url


logger = logging.getLogger(__name__)

API_BASE = "https://redfox.hk"
# Wide library ("广域库", verified live 2026-08): fresher data, newest-first
# ordering via sortType=2, offset paging. Parameters identify an account by
# wechat alias (account), wxId, or bizInfo — never by display name.
QUERY_WORK_LIST_URL = f"{API_BASE}/story/api/gzh/data/queryWorkList"
# Detail endpoint: full plain-text content for one work. Verified live:
# code 2000 = success, 3203 = "no data / bad param" (no credit charged).
QUERY_WORK_URL = f"{API_BASE}/story/api/gzh/data/workDetail"

# Both codes above verified live: 2000 = success, 3203 = "no data / bad
# param" with no credit charged — non-errors for callers.
SUCCESS_CODES = {0, 2000, 3203}
AUTH_ERROR_CODES = {3106}

# HTML snippets that indicate the content field carries markup instead of
# plain text and must be stripped before it is cached in the queue.
_SCRIPT_STYLE_PATTERN = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_BLOCK_TAGS = re.compile(
    r"</?(?:p|div|br|section|li|h[1-6]|blockquote|pre|tr)\b[^>]*>",
    re.IGNORECASE,
)
_MAX_CACHED_CONTENT_BYTES = 100 * 1024
# Untrusted-API text bounds: title/digest are echoed in terminals and stored
# in queue.json, so they are truncated and stripped of control characters and
# invisible Unicode tag characters (a prompt-injection vector for LLMs).
_MAX_TITLE_CHARS = 512
_MAX_DIGEST_CHARS = 2048
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_UNICODE_TAGS = re.compile("[\U000e0000-\U000e007f]")
_HTML_MARKER = re.compile(r"<\s*[a-zA-Z/!]")
_UNCLOSED_SCRIPT = re.compile(r"<(script|style)\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL)


def sanitize_text(value: Any, limit: int) -> str:
    """Truncate and strip unsafe characters from one untrusted API string."""
    text = _UNICODE_TAGS.sub("", _CONTROL_CHARS.sub("", str(value or "")))
    text = " ".join(text.split("\n")) if "\r" in text else text
    text = text.replace("\r", "")
    return text.strip()[:limit]


class RedfoxAPIError(RuntimeError):
    """Base error with protocol classification fields."""

    code = "REDFOX_API_ERROR"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details


class RedfoxAuthError(RedfoxAPIError):
    code = "REDFOX_AUTH"
    retryable = False


class RedfoxRateLimitError(RedfoxAPIError):
    code = "REDFOX_RATE_LIMITED"
    retryable = True


class RedfoxTransientError(RedfoxAPIError):
    code = "REDFOX_TRANSIENT"
    retryable = True


class RedfoxAccountAmbiguous(RedfoxAPIError):
    code = "REDFOX_ACCOUNT_AMBIGUOUS"
    retryable = False


def strip_html_to_text(content: str) -> str:
    """Convert an HTML-ish content field into readable plain text."""
    text = _SCRIPT_STYLE_PATTERN.sub("", content)
    if "<script" in text.casefold() or "<style" in text.casefold():
        # An unclosed script/style block has no end tag to match; everything
        # after it is suspect, so drop the tail rather than leak JS/CSS.
        text = _UNCLOSED_SCRIPT.sub("", text)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _TAG_PATTERN.sub("", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def clean_content(content: Any) -> Optional[str]:
    """Return cacheable plain text for an API content field, or None."""
    if not isinstance(content, str):
        return None
    # Only treat the body as HTML when a tag-like marker exists; plain text
    # containing comparison operators ("1<2 and 3>4") must pass unchanged.
    text = (
        strip_html_to_text(content)
        if _HTML_MARKER.search(content)
        else content.strip()
    )
    if not text:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_CACHED_CONTENT_BYTES:
        # Oversized bodies are truncated (with a marker) instead of dropped:
        # dropping made the article permanently unreadable while the error
        # suggested a retry that could never succeed.
        text = encoded[:_MAX_CACHED_CONTENT_BYTES].decode("utf-8", errors="ignore") + "\n[truncated]"
    return text


def _parse_publish_time(value: Any) -> int:
    """Best-effort epoch seconds from publishTime; 0 when unparseable.

    The API returns Beijing-time strings; parse against a fixed UTC+8 offset so
    the window boundary does not shift with the host timezone.
    """
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = time.strptime(text[:19], fmt)
            break
        except ValueError:
            continue
    else:
        return 0
    stamp = datetime(*parsed[:6], tzinfo=timezone(timedelta(hours=8)))
    return int(stamp.timestamp())


class RedfoxClient:
    """Thin, billing-aware wrapper around the redfox.hk article API."""

    def __init__(self, api_key: str, *, request_delay: float = 0):
        key = (api_key or "").strip()
        if not key:
            raise ValueError("redfox API key is required")
        self.session = new_session(
            {
                "Accept": "application/json",
                "X-API-Key": key,
            }
        )
        self.request_delay = max(0.0, min(float(request_delay), 60.0))
        self._last_request_at: float | None = None

    def close(self) -> None:
        self.session.close()

    def _post(self, url: str, payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                if self._last_request_at is not None:
                    remaining = self.request_delay - (time.monotonic() - self._last_request_at)
                    if remaining > 0:
                        time.sleep(remaining)
                self._last_request_at = time.monotonic()
                response = self.session.post(url, json=payload, timeout=(10, 30))
                if response.status_code in {401, 403}:
                    raise RedfoxAuthError(
                        "redfox API key was rejected; run the redfox key setup command "
                        "and provide a valid key",
                        details={"operation": operation, "http_status": response.status_code},
                    )
                if response.status_code == 429:
                    raise RedfoxRateLimitError(
                        "redfox API rate limit hit",
                        details={"operation": operation, "http_status": 429},
                    )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("response is not a JSON object")
                code = data.get("code", 0)
                try:
                    code = int(code)
                except (TypeError, ValueError):
                    code = -1
                if code not in SUCCESS_CODES:
                    message = str(data.get("msg", "unknown error"))[:200]
                    details = {"operation": operation, "api_code": code}
                    if code in AUTH_ERROR_CODES or "api key" in message.casefold():
                        raise RedfoxAuthError(
                            f"redfox API rejected the request: {message}", details=details
                        )
                    if "频繁" in message or "limit" in message.casefold():
                        raise RedfoxRateLimitError(
                            f"redfox API rate limited {operation}: {message}", details=details
                        )
                    raise RedfoxAPIError(
                        f"redfox {operation} failed ({code}): {message}", details=details
                    )
                return data
            except (RedfoxAuthError, RedfoxRateLimitError):
                raise
            except Exception as exc:
                if not is_transient_network_error(exc):
                    if isinstance(exc, RedfoxAPIError):
                        raise
                    raise RedfoxAPIError(
                        f"redfox {operation} failed: {type(exc).__name__}"
                    ) from exc
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RedfoxTransientError(
            f"redfox network request failed after 3 attempts: {type(last_error).__name__}",
            details={"operation": operation},
        )

    def query_work_list(
        self,
        *,
        account: str = "",
        offset: int = 0,
        count: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (one newest-first page of works, raw API code)."""
        account = str(account or "").strip()
        if not account:
            raise ValueError("the wide library requires the account wechat alias")
        payload = {
            "account": account,
            "offset": max(0, int(offset)),
            "count": max(1, min(int(count), 20)),
            "sortType": "2",
        }
        data = self._post(QUERY_WORK_LIST_URL, payload, operation="work_list")
        items = data.get("data")
        if isinstance(items, dict):
            items = items.get("list") or items.get("records") or items.get("rows")
        if not isinstance(items, list):
            items = []
        return items, int(data.get("code", 0) or 0)

    def query_work(self, work_uuid: str) -> tuple[dict[str, Any], int]:
        """Return (raw detail, raw API code) for one work.

        Code 3203 means the library has not crawled this work yet; the detail
        is empty but the call is a documented success (no credit charged).
        """
        work_uuid = str(work_uuid or "").strip()
        if not work_uuid:
            raise ValueError("work_uuid is required")
        data = self._post(
            QUERY_WORK_URL, {"workUuid": work_uuid}, operation="work_detail"
        )
        detail = data.get("data")
        return (detail if isinstance(detail, dict) else {}), int(data.get("code", 0) or 0)

    def fetch_content(self, work_uuid: str) -> Optional[str]:
        """Fetch and clean one article body; one paid call per invocation."""
        detail, _ = self.query_work(work_uuid)
        return clean_content(detail.get("content"))

    def list_articles(
        self,
        *,
        account: str = "",
        cutoff_epoch: int = 0,
        max_articles: int = 100,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Collect works into the lookback window, cheapest-first.

        sortType=2 orders pages newest-first (verified live), but ordering is
        not contractual, so within a page every item is examined instead of
        stopping at the first old one. Pagination stops when a whole page is
        older than the window, the server runs out of data, the collected
        limit is reached, or a hard page cap trips (defence against responses
        that never terminate the loop: pages of fully invalid items).
        Returns (articles, info) where info carries diagnostics for callers.
        """
        collected: list[dict[str, Any]] = []
        seen_links: set[str] = set()
        offset = 0
        pages = 0
        empty_reason = "exhausted"
        max_pages = (max_articles + page_size - 1) // page_size + 2
        while len(collected) < max_articles and pages < max_pages:
            items, api_code = self.query_work_list(
                account=account, offset=offset, count=page_size
            )
            pages += 1
            if not items:
                empty_reason = "no_data" if api_code == 3203 else "exhausted"
                break
            eligible = 0
            old_or_invalid = 0
            for raw in items:
                article = self.format_article(raw)
                if article is None:
                    old_or_invalid += 1
                    continue
                if article["link"] in seen_links:
                    # Server repeated a page (offset ignored); do not pay for
                    # the same items again.
                    old_or_invalid += 1
                    continue
                if (
                    cutoff_epoch
                    and article["update_time"]
                    and article["update_time"] < cutoff_epoch
                ):
                    old_or_invalid += 1
                    continue
                seen_links.add(article["link"])
                collected.append(article)
                eligible += 1
                if len(collected) >= max_articles:
                    break
            if len(items) < page_size:
                break
            if eligible == 0 and old_or_invalid == len(items):
                # Whole page outside the window or unusable: no later page can
                # help a newest-first feed, and a misbehaving feed must not
                # create an unbounded paid loop.
                empty_reason = "outside_window"
                break
            offset += len(items)
        info = {"pages": pages, "empty_reason": empty_reason, "api_code": api_code if pages else 0}
        return collected, info

    @staticmethod
    def format_article(raw: Any) -> Optional[dict[str, Any]]:
        """Normalize one raw work item; None when unusable."""
        if not isinstance(raw, dict):
            return None
        title = str(raw.get("title", "")).strip()
        link = str(raw.get("workUrl", raw.get("url", "")) or "").strip()
        if not title or not link:
            return None
        try:
            link = canonicalize_wechat_article_url(link)
        except (TypeError, ValueError):
            return None
        return {
            "title": sanitize_text(title, _MAX_TITLE_CHARS),
            "link": link,
            "digest": sanitize_text(
                raw.get("digest", raw.get("summary", "")), _MAX_DIGEST_CHARS
            ),
            # The list endpoint carries no body; work_uuid is the handle for a
            # lazily-fetched plain-text content (one paid detail call).
            "work_uuid": sanitize_text(raw.get("workUuid"), 64),
            "publish_time_raw": sanitize_text(raw.get("publishTime"), 32),
            "update_time": _parse_publish_time(raw.get("publishTime")),
        }
