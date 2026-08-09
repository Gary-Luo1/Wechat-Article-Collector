"""Browser-fingerprint HTTP client selection shared by WeChat network modules.

WeChat risk control fingerprints the TLS handshake (JA3) and header set, not
just the User-Agent. When ``curl_cffi`` is installed, this module returns a
session that impersonates a real Chrome browser (TLS, HTTP/2, and header
defaults). When it is missing, it falls back to plain ``requests`` so existing
installations keep working; that fallback still exposes the non-browser
fingerprint and should only be a degraded mode.
"""

from __future__ import annotations

import time

import requests

try:
    from curl_cffi import requests as curl_requests

    CURL_CFFI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    curl_requests = None
    CURL_CFFI_AVAILABLE = False

try:  # pragma: no cover - only exercised when curl_cffi is installed
    from curl_cffi.requests.exceptions import RequestsError as CurlRequestsError
except ImportError:  # pragma: no cover - default local fallback
    CurlRequestsError = ()


# Markers seen in WeChat risk-control / verification pages. A page containing
# these markers is not a normal article and must never be retried.
RISK_CONTROL_MARKERS = (
    "环境异常",
    "当前环境异常",
    "访问过于频繁",
    "访问频率过高",
    "操作过于频繁",
    "请在微信客户端打开",
    "微信安全验证",
)


_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
)


def new_session(headers: dict[str, str] | None = None) -> requests.Session:
    """Create a Chrome-impersonating session (or requests fallback) with headers."""
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


class RequestPacer:
    """Apply a bounded delay between outbound requests using monotonic time."""

    def __init__(self, request_delay: float = 0) -> None:
        self.request_delay = max(0.0, min(float(request_delay), 60.0))
        self._last_request_at: float | None = None

    def wait(self) -> None:
        if self._last_request_at is not None:
            remaining = self.request_delay - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()


def is_retryable_http_status(status_code: int) -> bool:
    """Return whether an HTTP response can be retried without changing intent."""
    return status_code in {500, 502, 503, 504}


def is_transient_network_error(exc: BaseException) -> bool:
    """Return True only for transport failures that are safe to retry."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if CurlRequestsError and isinstance(exc, CurlRequestsError):
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return isinstance(status_code, int) and is_retryable_http_status(status_code)
    return False


def looks_like_risk_control(html: str) -> bool:
    """Return True when the HTML looks like a WeChat risk-control page."""
    return any(marker in html for marker in RISK_CONTROL_MARKERS)
