"""Article URL identity rules shared by queue, discovery, parsing, and Feishu.

Stdlib-only by design: importing this module must never pull in the HTML
parser or any optional dependency. The parser module and the queue module
re-export these functions so callers keep stable names.
"""

from __future__ import annotations

import re
import urllib.parse


ALLOWED_HOST = "mp.weixin.qq.com"


def is_wechat_article_url(url: str) -> bool:
    """Return whether the URL is an exact-host WeChat article URL."""
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
    """Upgrade an exact-host HTTP article URL and enforce the allowlist."""
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
    if not is_wechat_article_url(url):
        raise ValueError("only https://mp.weixin.qq.com/s article URLs are allowed")
    return url


def normalize_article_url(url: str) -> str:
    """Normalize a URL into the queue identity key without collapsing articles."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("article URL must be a non-empty string")
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError("article URL must use http or https")
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
    if host == ALLOWED_HOST and path in {"/s", "/s/"}:
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        ordered = []
        for key in ("__biz", "mid", "sn", "idx"):
            if key in params and params[key]:
                ordered.append((key, params[key][0]))
        query = urllib.parse.urlencode(ordered)
    elif host == ALLOWED_HOST and path.startswith("/s/"):
        query = ""
    else:
        query = ""
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def upgrade_wechat_article_url(url: str) -> str:
    """Upgrade an exact-host http article URL to https without raising."""
    url = str(url or "").strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme == "http"
            and (parsed.hostname or "").lower() == ALLOWED_HOST
            and (parsed.path == "/s" or parsed.path.startswith("/s/"))
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
        ):
            return urllib.parse.urlunsplit(parsed._replace(scheme="https"))
    except (TypeError, UnicodeError, ValueError):
        pass
    return url
