"""Direct tests for the stdlib-only article URL identity module."""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest


class TestAllowlist:
    def test_accepts_wechat_article_urls(self):
        from url_identity import is_wechat_article_url

        assert is_wechat_article_url("https://mp.weixin.qq.com/s/abc")
        assert is_wechat_article_url("https://mp.weixin.qq.com/s?__biz=x")

    def test_rejects_non_wechat_urls(self):
        from url_identity import is_wechat_article_url

        assert not is_wechat_article_url("http://mp.weixin.qq.com/s/abc")
        assert not is_wechat_article_url("https://mp.weixin.qq.com.evil.test/s/abc")
        assert not is_wechat_article_url("https://example.com/s/abc")
        assert not is_wechat_article_url("https://mp.weixin.qq.com/other")

    @pytest.mark.parametrize(
        "url",
        [
            "https://mp.weixin.qq.com/s/a/../../x",
            "https://mp.weixin.qq.com/s/a/%2e%2e/x",
            "https://mp.weixin.qq.com/s/a/%252e%252e/x",
            "https://mp.weixin.qq.com/s/a/%5c..%5cx",
        ],
    )
    def test_rejects_path_escapes(self, url):
        from url_identity import is_wechat_article_url

        assert not is_wechat_article_url(url)


class TestCanonicalize:
    def test_upgrades_exact_http(self):
        from url_identity import canonicalize_wechat_article_url

        assert (
            canonicalize_wechat_article_url("http://mp.weixin.qq.com/s/abc")
            == "https://mp.weixin.qq.com/s/abc"
        )

    def test_keeps_https_unchanged(self):
        from url_identity import canonicalize_wechat_article_url

        url = "https://mp.weixin.qq.com/s?__biz=x&mid=1"
        assert canonicalize_wechat_article_url(url) == url

    def test_rejects_non_article_url(self):
        from url_identity import canonicalize_wechat_article_url

        with pytest.raises(ValueError):
            canonicalize_wechat_article_url("https://example.com/s/abc")


class TestNormalize:
    def test_tracking_parameters_are_order_independent(self):
        from url_identity import normalize_article_url

        first = normalize_article_url(
            "https://mp.weixin.qq.com/s?__biz=x&mid=1&sn=a&scene=1"
        )
        second = normalize_article_url(
            "https://mp.weixin.qq.com/s?sn=a&mid=1&__biz=x&chksm=ignored"
        )
        assert first == second

    def test_slash_path_identity_drops_query(self):
        from url_identity import normalize_article_url

        assert normalize_article_url(
            "https://mp.weixin.qq.com/s/abc?foo=bar"
        ) == "https://mp.weixin.qq.com/s/abc"

    def test_rejects_empty_or_bad_urls(self):
        from url_identity import normalize_article_url

        with pytest.raises(ValueError):
            normalize_article_url("")
        with pytest.raises(ValueError):
            normalize_article_url("not a url")


class TestUpgrade:
    def test_upgrades_exact_http_article(self):
        from url_identity import upgrade_wechat_article_url

        assert (
            upgrade_wechat_article_url("http://mp.weixin.qq.com/s?__biz=x")
            == "https://mp.weixin.qq.com/s?__biz=x"
        )
        assert (
            upgrade_wechat_article_url("http://mp.weixin.qq.com/s/abc")
            == "https://mp.weixin.qq.com/s/abc"
        )

    def test_leaves_other_urls_unchanged(self):
        from url_identity import upgrade_wechat_article_url

        assert (
            upgrade_wechat_article_url("https://mp.weixin.qq.com/s/abc")
            == "https://mp.weixin.qq.com/s/abc"
        )
        assert (
            upgrade_wechat_article_url("http://example.com/s/abc")
            == "http://example.com/s/abc"
        )
        assert upgrade_wechat_article_url("") == ""
        assert upgrade_wechat_article_url(None) == ""


def test_identity_module_works_without_parser_or_bs4(
    monkeypatch: pytest.MonkeyPatch,
):
    """Block the parser module so a broken install cannot hide behind it."""
    original_import = builtins.__import__
    sys.modules.pop("url_identity", None)

    def block_parser(name, *args, **kwargs):
        if name in {"article_reader", "bs4"}:
            raise ModuleNotFoundError(f"{name} is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_parser)
    url_identity = importlib.import_module("url_identity")

    assert url_identity.is_wechat_article_url("https://mp.weixin.qq.com/s/abc")
    assert not url_identity.is_wechat_article_url("https://example.com/s/abc")
    assert (
        url_identity.canonicalize_wechat_article_url(
            "http://mp.weixin.qq.com/s/abc"
        )
        == "https://mp.weixin.qq.com/s/abc"
    )
    assert (
        url_identity.normalize_article_url(
            "https://mp.weixin.qq.com/s?sn=a&__biz=x"
        )
        == "https://mp.weixin.qq.com/s?__biz=x&sn=a"
    )
    assert (
        url_identity.upgrade_wechat_article_url("http://mp.weixin.qq.com/s/x")
        == "https://mp.weixin.qq.com/s/x"
    )
