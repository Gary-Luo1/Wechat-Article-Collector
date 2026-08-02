"""Conservative wrapper around the private WeChat Official Account backend."""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from url_identity import upgrade_wechat_article_url


logger = logging.getLogger(__name__)


class WeChatAPIError(RuntimeError):
    pass


class WeChatTokenExpired(WeChatAPIError):
    pass


class WeChatCookieExpired(WeChatAPIError):
    pass


class WeChatRateLimitError(WeChatAPIError):
    pass


class WeChatCredentialContextError(WeChatAPIError):
    pass


class WeChatAPI:
    SEARCH_BIZ_URL = "https://mp.weixin.qq.com/cgi-bin/searchbiz"
    APPMSG_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"

    def __init__(
        self,
        cookie: str,
        token: str,
        proxies: Optional[dict] = None,
        request_delay: float = 0,
    ):
        if not cookie.strip() or not token.strip():
            raise ValueError("WeChat cookie and token are required")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
                "Cookie": cookie,
                "Accept": "application/json,text/plain,*/*",
            }
        )
        self.base_params = {"lang": "zh_CN", "f": "json", "token": token}
        self.proxies = proxies or {}
        self.request_delay = max(0.0, min(float(request_delay), 60.0))
        self._last_request_at = 0.0
        self.cookie_names = {
            name.strip()
            for part in cookie.split(";")
            for name, separator, _ in [part.partition("=")]
            if separator and name.strip()
        }

    @staticmethod
    def _raise_api_error(
        data: dict, operation: str, cookie_names: set[str] | None = None
    ) -> None:
        base_response = data.get("base_resp", {})
        ret = base_response.get("ret", data.get("ret", data.get("errcode", 0)))
        try:
            ret = int(ret)
        except (TypeError, ValueError):
            pass
        if ret == 0:
            return
        message = str(
            base_response.get("err_msg", data.get("errmsg", data.get("msg", "unknown error")))
        )[:200]
        lower = message.casefold()
        if "invalid args" in lower or "invalid argument" in lower:
            missing = sorted({"rand_info", "slave_bizuin"} - (cookie_names or set()))
            if missing:
                raise WeChatCredentialContextError(
                    "WeChat rejected the request because the Cookie appears incomplete "
                    f"(missing {', '.join(missing)}). Sign in at https://mp.weixin.qq.com/, "
                    "open browser developer tools > Application > Storage > Cookies > "
                    "https://mp.weixin.qq.com/, then copy every cookie row."
                )
            raise WeChatCredentialContextError(
                "WeChat rejected the credential context. Sign in at "
                "https://mp.weixin.qq.com/, then refresh the complete cookie set from "
                "Application storage and the numeric token from the current page URL "
                "(never /wxamp/), and retry immediately."
            )
        if ret == 200003 or "token expired" in lower or "invalid token" in lower:
            raise WeChatTokenExpired(
                "WeChat token expired. Refresh the complete cookie set from Application "
                "storage and the numeric token from the current authenticated page URL."
            )
        if ret in {1, 100003, 200013}:
            raise WeChatCookieExpired("WeChat session expired; sign in again locally")
        if ret in {200002, 200007, 200008}:
            raise WeChatRateLimitError(f"WeChat rate limited {operation}: {message}")
        raise WeChatAPIError(f"WeChat {operation} failed ({ret}): {message}")

    def _get(self, url: str, params: dict, retries: int = 3) -> dict:
        merged = {**self.base_params, **params}
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                remaining = self.request_delay - (time.monotonic() - self._last_request_at)
                if remaining > 0:
                    time.sleep(remaining)
                response = self.session.get(
                    url,
                    params=merged,
                    proxies=self.proxies,
                    timeout=(10, 30),
                )
                self._last_request_at = time.monotonic()
                if response.status_code in {401, 403}:
                    raise WeChatCookieExpired(
                        "WeChat session expired; sign in at https://mp.weixin.qq.com/ and "
                        "refresh the cookie set from Application storage and token from "
                        "the current authenticated page URL."
                    )
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as exc:
                    if "html" in response.headers.get("Content-Type", "").casefold():
                        raise WeChatCookieExpired(
                            "WeChat returned a sign-in page; sign in again and refresh "
                            "the cookie set from Application storage and token from the "
                            "current authenticated page URL."
                        ) from exc
                    raise
                if not isinstance(data, dict):
                    raise ValueError("response is not a JSON object")
                return data
            except (WeChatCookieExpired, WeChatTokenExpired, WeChatCredentialContextError):
                raise
            except (requests.RequestException, ValueError) as exc:
                # Do not log exception URLs: the query string contains the token.
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(2**attempt)
        raise WeChatAPIError(
            f"WeChat network request failed after {retries} attempts: "
            f"{type(last_error).__name__}"
        )

    def search_account(self, keyword: str, begin: int = 0, count: int = 5) -> list[dict]:
        data = self._get(
            self.SEARCH_BIZ_URL,
            {
                "action": "search_biz",
                "query": keyword,
                "begin": str(begin),
                "count": str(count),
                "ajax": "1",
            },
        )
        self._raise_api_error(data, "account search", self.cookie_names)
        result = data.get("list", [])
        return result if isinstance(result, list) else []

    def list_articles(
        self, fakeid: str, begin: int = 0, count: int = 5
    ) -> tuple[list[dict], int]:
        data = self._get(
            self.APPMSG_URL,
            {
                "action": "list_ex",
                "fakeid": fakeid,
                "begin": str(begin),
                "count": str(count),
                "type": "9",
                "query": "",
                "ajax": "1",
            },
        )
        self._raise_api_error(data, "article listing", self.cookie_names)
        articles = data.get("app_msg_list", [])
        count_value = data.get("app_msg_cnt", 0)
        return (articles if isinstance(articles, list) else [], int(count_value or 0))

    def get_account(self, *, name: str = "", alias: str = "") -> Optional[dict]:
        query = alias or name
        if not query:
            return None
        results = self.search_account(query)
        exact = [
            item
            for item in results
            if (alias and item.get("alias") == alias)
            or (name and item.get("nickname") == name)
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise WeChatAPIError(f"multiple exact account matches for {query!r}")
        return None

    @staticmethod
    def format_article(article: dict) -> dict:
        link = upgrade_wechat_article_url(str(article.get("link", "")).strip())
        return {
            "aid": str(article.get("aid", "")),
            "appmsgid": str(article.get("appmsgid", "")),
            "title": str(article.get("title", "")).strip(),
            "link": link,
            "digest": str(article.get("digest", "")).strip(),
            "cover": str(article.get("cover", "")).strip(),
            "update_time": int(article.get("update_time", 0) or 0),
        }
