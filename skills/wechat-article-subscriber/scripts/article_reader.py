"""Fetch and extract WeChat article text with strict network boundaries."""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

from http_client import (
    RequestPacer,
    is_transient_network_error,
    is_retryable_http_status,
    looks_like_risk_control,
    new_session,
)
from url_identity import (
    canonicalize_wechat_article_url,
    is_wechat_article_url,
)


# Backward-compatible alias: the allowlist rule now lives in url_identity.
is_wechat_article = is_wechat_article_url

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_REDIRECTS = 3


class ArticleFetchError(ValueError):
    """Base error for article reads with a stable protocol classification."""

    code = "ARTICLE_FETCH_FAILED"
    retryable = False
    next_action = "read_article_again"


class WeChatRiskControlError(ArticleFetchError):
    """WeChat served a risk-control/verification page instead of an article."""

    code = "ARTICLE_RISK_CONTROL"
    next_action = "wait_before_retry"


class ArticleContentError(ArticleFetchError):
    """The response is reachable but is not a valid readable article."""

    code = "ARTICLE_CONTENT_INVALID"
    next_action = "open_article_in_wechat"


class ArticleResponseTooLargeError(ArticleFetchError):
    """The article exceeded the configured bounded response size."""

    code = "ARTICLE_RESPONSE_TOO_LARGE"
    next_action = "open_article_in_wechat"


class ArticleHTTPError(ArticleFetchError):
    """WeChat returned a non-risk-control HTTP response error."""

    code = "ARTICLE_HTTP_ERROR"

    def __init__(self, status_code: int):
        super().__init__(f"WeChat article request failed (HTTP {status_code})")
        self.status_code = status_code
        self.retryable = is_retryable_http_status(status_code)
        self.next_action = "retry_with_backoff" if self.retryable else "open_article_in_wechat"


class ArticleNetworkError(ArticleFetchError):
    """A bounded sequence of retryable transport attempts was exhausted."""

    code = "ARTICLE_TRANSIENT"
    retryable = True
    next_action = "retry_with_backoff"


def _validate_url(url: str) -> str:
    return canonicalize_wechat_article_url(url)


def _get_with_safe_redirects(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
    pacer: RequestPacer | None = None,
) -> requests.Response:
    current = _validate_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        if pacer is not None:
            pacer.wait()
        response = session.get(
            current,
            headers=headers,
            timeout=(10, timeout),
            allow_redirects=False,
            stream=True,
        )
        # curl_cffi does not expose requests-style redirect flags; treat a
        # Location header as the portable redirect signal.
        if getattr(response, "is_redirect", False) or getattr(
            response, "is_permanent_redirect", False
        ) or response.headers.get("Location"):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise requests.RequestException("redirect response has no Location header")
            current = _validate_url(urllib.parse.urljoin(current, location))
            continue
        return response
    raise requests.TooManyRedirects("too many article redirects")


def _meta_content(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        element = soup.find("meta", attrs={"property": key}) or soup.find(
            "meta", attrs={"name": key}
        )
        if element is not None:
            value = str(element.get("content", "")).strip()
            if value:
                return value
    return ""


def _element_text(soup: BeautifulSoup, *selectors: str) -> str:
    for selector in selectors:
        element = soup.select_one(selector)
        if element is not None:
            value = element.get_text(" ", strip=True)
            if value:
                return value
    return ""


def _script_value(html: str, names: tuple[str, ...]) -> str:
    for name in names:
        match = re.search(
            rf"(?<![\w$])(?:var\s+)?{re.escape(name)}\s*=\s*(['\"])(.*?)\1",
            html,
            flags=re.DOTALL,
        )
        if match:
            return match.group(2).strip()
    return ""


def _published_timestamp(soup: BeautifulSoup, html: str) -> int:
    raw = _meta_content(
        soup,
        "article:published_time",
        "og:article:published_time",
    ) or _script_value(html, ("ct", "publish_time"))
    if raw.isdigit():
        value = int(raw)
        return value // 1000 if value > 10_000_000_000 else value
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            pass
    return 0


def _extract_article_document(html: str, url: str, max_chars: int) -> dict[str, Any]:
    if looks_like_risk_control(html):
        raise WeChatRiskControlError(
            "WeChat returned a risk-control/verification page instead of the article"
        )
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", id="js_content")
    if container is None:
        raise ArticleContentError("WeChat article container was not found")
    for element in container(["script", "style", "noscript"]):
        element.decompose()
    text = container.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ArticleContentError("WeChat article text is empty")
    parsed = urllib.parse.urlsplit(url)
    biz = urllib.parse.parse_qs(parsed.query).get("__biz", [""])[0] or _script_value(
        html, ("biz", "__biz")
    )
    title = _meta_content(soup, "og:title") or _element_text(
        soup, "#activity-name", "h1.rich_media_title"
    )
    account = _meta_content(soup, "og:article:author", "author") or _element_text(
        soup, "#js_name", ".rich_media_meta_nickname"
    )
    if not account:
        account = _script_value(html, ("nickname",))
    digest = _meta_content(soup, "og:description", "description")
    return {
        "title": title[:500],
        "account": account[:200],
        "account_id": biz[:500],
        "digest": digest[:2000],
        "update_time": _published_timestamp(soup, html),
        "link": url,
        "text": text[:max_chars],
    }


def fetch_article(
    url: str,
    timeout: int = 30,
    retries: int = 2,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_chars: int = MAX_TEXT_CHARS,
    session: requests.Session | None = None,
    pacer: RequestPacer | None = None,
) -> dict[str, Any]:
    """Return a bounded article document or raise a typed fetch error."""
    url = _validate_url(url)
    headers = {
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    owned_session = session is None
    client = session or new_session(headers)
    last_error: Exception | None = None
    try:
        for attempt in range(retries + 1):
            response: requests.Response | None = None
            try:
                response = _get_with_safe_redirects(
                    client, url, headers=headers, timeout=timeout, pacer=pacer
                )
                status_code = getattr(response, "status_code", None)
                if isinstance(status_code, int):
                    if status_code in {403, 429}:
                        raise WeChatRiskControlError(
                            f"WeChat blocked the request (HTTP {status_code})"
                        )
                    if status_code >= 400:
                        raise ArticleHTTPError(status_code)
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise ArticleResponseTooLargeError("article response exceeds size limit")
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise ArticleResponseTooLargeError("article response exceeds size limit")
                encoding = (
                    getattr(response, "encoding", None)
                    or getattr(response, "apparent_encoding", None)
                    or "utf-8"
                )
                html = bytes(payload).decode(encoding, errors="replace")
                final_url = getattr(response, "url", "")
                if not isinstance(final_url, str) or not final_url:
                    final_url = url
                final_url = _validate_url(final_url)
                return _extract_article_document(html, final_url, max_chars)
            except ArticleFetchError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
            except Exception as exc:
                if not is_transient_network_error(exc):
                    raise
                last_error = exc
            finally:
                if response is not None:
                    response.close()
            if attempt < retries:
                time.sleep(2**attempt)
        raise ArticleNetworkError(
            f"article fetch failed after {retries + 1} attempts: {type(last_error).__name__}"
        )
    finally:
        if owned_session:
            client.close()


def fetch_article_text(
    url: str,
    timeout: int = 30,
    retries: int = 2,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_chars: int = MAX_TEXT_CHARS,
    session: requests.Session | None = None,
    pacer: RequestPacer | None = None,
) -> str:
    """Compatibility wrapper returning only validated bounded article text."""
    article = fetch_article(
        url,
        timeout,
        retries,
        max_bytes,
        max_chars,
        session=session,
        pacer=pacer,
    )
    return str(article["text"])
