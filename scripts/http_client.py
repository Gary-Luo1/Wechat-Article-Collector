"""Browser-fingerprint HTTP client selection for outbound API sessions.

``curl_cffi`` (when installed) impersonates a real Chrome browser for TLS,
HTTP/2, and header defaults; a plain ``requests`` fallback keeps existing
runtimes working.
"""

from __future__ import annotations


from typing import Any

import requests

try:
    from curl_cffi import requests as curl_requests

    CURL_CFFI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    curl_requests = None
    CURL_CFFI_AVAILABLE = False

try:  # pragma: no cover - only exercised when curl_cffi is installed
    from curl_cffi.requests.exceptions import (
        ConnectionError as CurlConnectionError,
        RequestException as CurlRequestException,
        Timeout as CurlTimeout,
    )

    CurlTransientErrors: tuple[type[BaseException], ...] = (
        CurlRequestException,
        CurlConnectionError,
        CurlTimeout,
    )
except ImportError:  # pragma: no cover - default local fallback
    CurlTransientErrors = ()


_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
)


def new_session(headers: dict[str, str] | None = None) -> Any:
    """Create a Chrome-impersonating session (or requests fallback) with headers.

    The curl_cffi session is duck-type compatible with requests.Session but is
    not a subclass, so the return type is deliberately Any.
    """
    session = (
        curl_requests.Session(impersonate="chrome")
        if CURL_CFFI_AVAILABLE
        else requests.Session()
    )
    merged = dict(headers or {})
    if not CURL_CFFI_AVAILABLE and "User-Agent" not in merged:
        # Impersonation supplies a matching UA; the requests fallback needs one.
        merged["User-Agent"] = _FALLBACK_USER_AGENT
    session.headers.update(merged)
    return session


def is_retryable_http_status(status_code: int) -> bool:
    """Return whether an HTTP response can be retried without changing intent."""
    return status_code in {500, 502, 503, 504}


def is_transient_network_error(exc: BaseException) -> bool:
    """Return True only for transport failures that are safe to retry."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if CurlTransientErrors and isinstance(exc, CurlTransientErrors):
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return isinstance(status_code, int) and is_retryable_http_status(status_code)
    return False
