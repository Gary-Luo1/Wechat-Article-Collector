"""Fetch and extract WeChat article text with strict network boundaries."""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)
ALLOWED_HOST = "mp.weixin.qq.com"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_REDIRECTS = 3


def is_wechat_article(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        raw_path = parsed.path
        decoded_path = raw_path
        for _ in range(4):
            next_path = urllib.parse.unquote(decoded_path)
            if next_path == decoded_path:
                break
            decoded_path = next_path
        else:
            return False
        segments = decoded_path.replace("\\", "/").split("/")
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() == ALLOWED_HOST
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and "\\" not in raw_path
            and "\\" not in decoded_path
            and not any(segment in {".", ".."} for segment in segments)
            and not any(ord(character) < 32 for character in decoded_path)
            and (decoded_path == "/s" or decoded_path.startswith("/s/"))
        )
    except (TypeError, UnicodeError, ValueError):
        return False


def canonicalize_wechat_article_url(url: str) -> str:
    """Upgrade an exact-host HTTP API result before any network request."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme == "http"
            and (parsed.hostname or "").lower() == ALLOWED_HOST
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
        ):
            parsed = parsed._replace(scheme="https")
            url = urllib.parse.urlunsplit(parsed)
    except (TypeError, UnicodeError, ValueError):
        pass
    if not is_wechat_article(url):
        raise ValueError("only https://mp.weixin.qq.com/s article URLs are allowed")
    return url


def _validate_url(url: str) -> str:
    return canonicalize_wechat_article_url(url)


def _get_with_safe_redirects(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str],
    timeout: int,
) -> requests.Response:
    current = _validate_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        response = session.get(
            current,
            headers=headers,
            timeout=(10, timeout),
            allow_redirects=False,
            stream=True,
        )
        if response.is_redirect or response.is_permanent_redirect:
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
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", id="js_content")
    if container is None:
        raise ValueError("WeChat article container was not found")
    for element in container(["script", "style", "noscript"]):
        element.decompose()
    text = container.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("WeChat article text is empty")
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
) -> Optional[dict[str, Any]]:
    """Return bounded article metadata/text or None after retry exhaustion."""
    url = _validate_url(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ),
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "text/html,application/xhtml+xml",
    }
    session = requests.Session()
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        response: requests.Response | None = None
        try:
            response = _get_with_safe_redirects(
                session, url, headers=headers, timeout=timeout
            )
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError("article response exceeds size limit")
            payload = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    raise ValueError("article response exceeds size limit")
            encoding = response.encoding or response.apparent_encoding or "utf-8"
            html = bytes(payload).decode(encoding, errors="replace")
            final_url = getattr(response, "url", "")
            if not isinstance(final_url, str) or not final_url:
                final_url = url
            final_url = _validate_url(final_url)
            return _extract_article_document(html, final_url, max_chars)
        except (requests.RequestException, UnicodeError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)
        finally:
            if response is not None:
                response.close()
    logger.error(
        "article fetch failed after %d attempts: %s",
        retries + 1,
        type(last_error).__name__,
    )
    return None


def fetch_article_text(
    url: str,
    timeout: int = 30,
    retries: int = 2,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_chars: int = MAX_TEXT_CHARS,
) -> Optional[str]:
    """Compatibility wrapper returning only bounded article text."""
    article = fetch_article(url, timeout, retries, max_bytes, max_chars)
    return str(article["text"]) if article else None
