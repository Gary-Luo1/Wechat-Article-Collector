"""Tests for the redfox.hk article data source."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wechat-article-subscriber" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from config_store import ConfigError, validate_config  # noqa: E402
from redfox_client import (  # noqa: E402
    RedfoxAPIError,
    RedfoxAuthError,
    RedfoxClient,
    RedfoxRateLimitError,
    clean_content,
    strip_html_to_text,
)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        result = self.responses.pop(0)
        return result if isinstance(result, _FakeResponse) else _FakeResponse(payload=result)

    def close(self):
        pass


def _client(monkeypatch, responses):
    client = RedfoxClient("test-key-12345678")
    session = _FakeSession(responses)
    monkeypatch.setattr(client, "session", session)
    return client, session


def test_missing_key_rejected():
    with pytest.raises(ValueError):
        RedfoxClient("  ")


def test_auth_error_classified(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [{"code": 3106, "msg": "缺少API Key，请在请求头中传入X-API-Key"}],
    )
    with pytest.raises(RedfoxAuthError) as exc_info:
        client.query_work_list(account="rmrb")
    assert exc_info.value.code == "REDFOX_AUTH"
    assert not exc_info.value.retryable


def test_http_401_maps_to_auth(monkeypatch):
    client, _ = _client(monkeypatch, [_FakeResponse(status_code=401, payload={})])
    with pytest.raises(RedfoxAuthError):
        client.query_work_list(account="rmrb")


def test_rate_limit_classified_and_retryable(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [
            {"code": 1001, "msg": "请求过于频繁"},
        ],
    )
    with pytest.raises(RedfoxRateLimitError) as exc_info:
        client.query_work_list(account="rmrb")
    assert exc_info.value.retryable


def test_api_error_code_mapping(monkeypatch):
    client, _ = _client(monkeypatch, [{"code": 500, "msg": "server busy"}])
    with pytest.raises(RedfoxAPIError):
        client.query_work_list(account="rmrb")


def test_pagination_stops_at_cutoff(monkeypatch):
    now = int(time.time())
    page1 = [
        {"title": "new", "workUrl": "http://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=aaa",
         "workUuid": "U1",
         "publishTime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))},
        {"title": "old", "workUrl": "http://mp.weixin.qq.com/s?__biz=1&mid=2&idx=1&sn=bbb",
         "workUuid": "U2",
         "publishTime": "2020-01-01 00:00:00"},
    ]
    client, session = _client(monkeypatch, [{"code": 2000, "data": {"list": page1}}])
    articles, info = client.list_articles(account="rmrb", cutoff_epoch=now - 3600, max_articles=10)
    assert [item["title"] for item in articles] == ["new"]
    assert info["pages"] == 1
    assert info["empty_reason"] == "exhausted"  # short page (< page_size) ends pagination
    # No second page request once the oldest item is before the cutoff.
    assert len(session.calls) == 1


def test_format_article_requires_title_and_link(monkeypatch):
    client, _ = _client(monkeypatch, [{"code": 2000, "data": {"list": [{"title": "no url"}]}}])
    assert client.query_work_list(account="x") == ([{"title": "no url"}], 2000)
    assert RedfoxClient.format_article({"title": "no url"}) is None


def test_clean_content_strips_html_and_bounds_size():
    assert strip_html_to_text("<p>你好</p><div>世界</div>") == "你好\n\n世界"
    assert clean_content("<p>正文</p>") == "正文"
    assert clean_content("plain text") == "plain text"
    assert clean_content("") is None
    assert clean_content(None) is None
    # Oversized bodies are truncated (with a marker) instead of dropped.
    oversized = clean_content("字" * 200001)
    assert oversized is not None and oversized.endswith("\n[truncated]")


def test_article_source_setting_removed():
    # settings.article_source and the wechat section were removed with the
    # direct-connection data source; redfox.api_key is the only credential.
    config = validate_config({"version": 10})
    assert "article_source" not in config["settings"]
    assert "wechat" not in config
    assert config["redfox"]["api_key"] == ""
    with pytest.raises(ConfigError):
        validate_config({"version": 10, "settings": {"min_score": 100}})


def test_validate_config_has_no_wechat_gate():
    import inspect

    assert "require_wechat" not in inspect.signature(validate_config).parameters
    # A redfox-only config validates without any wechat credentials.
    validate_config(
        {
            "version": 10,
            "redfox": {"api_key": "k"},
            "subscriptions": [{"name": "人民日报"}],
        }
    )


def test_script_and_style_blocks_removed():
    assert (
        strip_html_to_text('<p>正文</p><script>alert("x")</script><style>.a{}</style>')
        == "正文"
    )


def test_request_payload_includes_count_and_sort(monkeypatch):
    client, session = _client(monkeypatch, [{"code": 2000, "data": {"list": []}}])
    client.query_work_list(account="rmrb", offset=20, count=5)
    sent = session.calls[0]["json"]
    assert sent == {"account": "rmrb", "offset": 20, "count": 5, "sortType": "2"}


def test_all_invalid_pages_never_loop_unbounded(monkeypatch):
    # Every page is full-sized but contains only unusable items; the loop must
    # stop after one such page, not page forever.
    page = [{"title": "", "workUrl": ""}] * 20
    client, session = _client(
        monkeypatch,
        [{"code": 2000, "data": {"list": page}}] * 10,
    )
    articles, info = client.list_articles(account="rmrb", max_articles=100)
    assert articles == []
    assert info["empty_reason"] == "outside_window"
    assert len(session.calls) == 1


def test_duplicate_pages_do_not_double_charge_items(monkeypatch):
    import time as _t
    item = {"title": "t", "workUrl": "http://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=dup",
            "workUuid": "U",
            "publishTime": _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time()))}
    client, session = _client(
        monkeypatch,
        [{"code": 2000, "data": {"list": [item] * 20}}] * 5,
    )
    import time as _time
    articles, _ = client.list_articles(
        account="rmrb", cutoff_epoch=int(_time.time()) - 3600, max_articles=100
    )
    assert len(articles) == 1  # duplicates collapsed
    assert len(session.calls) == 2  # second page all-duplicate -> stop


def test_publish_time_accepts_iso_and_minute_precision():
    from redfox_client import _parse_publish_time
    assert _parse_publish_time("2026-08-30T10:30:00") == _parse_publish_time("2026-08-30 10:30:00")
    assert _parse_publish_time("2026-08-30 10:30") == _parse_publish_time("2026-08-30 10:30:00")
    assert _parse_publish_time("garbage") == 0
    assert _parse_publish_time("") == 0


def test_title_digest_sanitized_and_truncated():
    article = RedfoxClient.format_article(
        {
            "title": "标\x00题\uE0041" + "长" * 600,
            "workUrl": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=clean",
            "summary": "摘\x1b[31m要" + "文" * 3000,
            "workUuid": "U1",
            "publishTime": "2026-08-30 10:00:00",
        }
    )
    assert "\x00" not in article["title"] and "\x1b" not in article["digest"]
    assert "\U000e0041" not in article["title"]
    assert len(article["title"]) == 512
    assert len(article["digest"]) == 2048


def test_oversized_content_truncated_not_dropped():
    text = clean_content("字" * 200001)
    assert text is not None
    assert text.endswith("\n[truncated]")
    assert len(text.encode("utf-8")) < 110 * 1024


def test_plain_text_with_angle_brackets_untouched():
    assert clean_content("1<2 且 3>4 成立") == "1<2 且 3>4 成立"


def test_unclosed_script_block_dropped():
    assert strip_html_to_text("<p>正文</p><script>var x=1;") == "正文"
