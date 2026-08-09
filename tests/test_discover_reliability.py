from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))


def _config() -> dict:
    from config_store import DEFAULT_CONFIG

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["wechat"] = {"cookie": "cookie", "token": "token"}
    config["settings"]["request_delay"] = 0
    config["subscriptions"] = [{"name": "First", "biz": "biz-first"}, {"name": "Second", "biz": "biz-second"}]
    return config


class _API:
    def list_articles(self, biz: str, *, begin: int, count: int):
        from wechat_api import WeChatAPIError

        if biz == "biz-second":
            raise WeChatAPIError("second account failed")
        return ([{"title": "First article", "link": "https://mp.weixin.qq.com/s/first", "update_time": 1_900_000_000}], 1)

    @staticmethod
    def format_article(raw: dict) -> dict:
        return {"title": raw["title"], "link": raw["link"], "digest": "", "update_time": raw["update_time"]}


class _AllSuccessAPI:
    def list_articles(self, biz: str, *, begin: int, count: int):
        return ([{"title": f"{biz} article", "link": f"https://mp.weixin.qq.com/s/{biz}", "update_time": 1_900_000_000}], 1)

    @staticmethod
    def format_article(raw: dict) -> dict:
        return {"title": raw["title"], "link": raw["link"], "digest": "", "update_time": raw["update_time"]}


class _ExternalLinkAPI(_AllSuccessAPI):
    def list_articles(self, biz: str, *, begin: int, count: int):
        return (
            [
                {"title": "External", "link": "https://example.com/not-an-article", "update_time": 1_900_000_000},
                {"title": "WeChat", "link": "https://mp.weixin.qq.com/s/valid", "update_time": 1_900_000_000},
            ],
            2,
        )


class _MalformedArticleAPI(_AllSuccessAPI):
    def list_articles(self, biz: str, *, begin: int, count: int):
        return (
            [
                {"title": "Malformed", "link": "https://mp.weixin.qq.com/s/bad", "update_time": 1_900_000_000},
                {"title": f"Valid {biz}", "link": f"https://mp.weixin.qq.com/s/{biz}", "update_time": 1_900_000_000},
            ],
            2,
        )

    @staticmethod
    def format_article(raw: dict) -> dict:
        if raw["title"] == "Malformed":
            raise ValueError("invalid update_time")
        return _AllSuccessAPI.format_article(raw)


class _FirstAccountBlockedAPI(_AllSuccessAPI):
    def list_articles(self, biz: str, *, begin: int, count: int):
        from wechat_api import WeChatAPIError

        raise WeChatAPIError("first account failed")


class _AccessRestrictedAPI(_AllSuccessAPI):
    def list_articles(self, biz: str, *, begin: int, count: int):
        from wechat_api import WeChatAccessRestricted

        raise WeChatAccessRestricted(
            "WeChat rejected article_listing (HTTP 403)",
            details={"operation": "article_listing", "http_status": 403},
        )


class _AccountLocalAPI(_AllSuccessAPI):
    def get_account(self, *, name: str, alias: str):
        return None


class _ResolvingAPI(_AllSuccessAPI):
    def get_account(self, *, name: str, alias: str):
        return {"fakeid": "resolved-biz"}


def test_discovery_keeps_earlier_account_articles_when_later_account_blocks(monkeypatch, capsys):
    import discover_only
    from config_store import save_config
    from queue_helpers import get_pending

    save_config(_config())
    monkeypatch.setattr(discover_only, "WeChatAPI", lambda *args, **kwargs: _API())

    assert discover_only.main(["--format", "json", "--hours", "99999"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WECHAT_API_ERROR"
    assert payload["meta"] == {
        "partial": True,
        "queued": 1,
        "completed_accounts": 1,
        "skipped_invalid": 0,
        "blocking_account": "Second",
    }
    assert [item["title"] for item in get_pending()] == ["First article"]
    assert "cookie" not in json.dumps(payload)
    assert "token" not in json.dumps(payload)

    assert discover_only.main(["--format", "json", "--hours", "99999"]) == 1
    assert len(get_pending()) == 1


def test_discovery_reports_full_success_and_queues_each_account(monkeypatch, capsys):
    import discover_only
    from config_store import save_config
    from queue_helpers import get_pending

    save_config(_config())
    monkeypatch.setattr(discover_only, "WeChatAPI", lambda *args, **kwargs: _AllSuccessAPI())

    assert discover_only.main(["--format", "json", "--hours", "99999"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["queued"] == 2
    assert [item["status"] for item in payload["data"]["accounts"]] == ["ok", "ok"]
    assert len(get_pending()) == 2


def test_discovery_skips_non_wechat_links_before_queueing(monkeypatch, capsys):
    import discover_only
    from config_store import save_config
    from queue_helpers import get_pending

    config = _config()
    config["subscriptions"] = [{"name": "First", "biz": "biz-first"}]
    save_config(config)
    monkeypatch.setattr(discover_only, "WeChatAPI", lambda *args, **kwargs: _ExternalLinkAPI())

    assert discover_only.main(["--format", "json", "--hours", "99999"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["queued"] == 1
    assert payload["data"]["accounts"][0]["invalid"] == 1
    assert [item["title"] for item in get_pending()] == ["WeChat"]


def test_discovery_skips_malformed_article_and_continues(monkeypatch, capsys):
    import discover_only
    from config_store import save_config
    from queue_helpers import get_pending

    save_config(_config())
    monkeypatch.setattr(discover_only, "WeChatAPI", lambda *args, **kwargs: _MalformedArticleAPI())

    assert discover_only.main(["--format", "json", "--hours", "99999"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["queued"] == 2
    assert [item["invalid"] for item in payload["data"]["accounts"]] == [1, 1]
    assert [item["status"] for item in payload["data"]["accounts"]] == ["ok", "ok"]
    assert len(get_pending()) == 2


def test_discovery_does_not_call_zero_progress_a_partial_success(monkeypatch, capsys):
    import discover_only
    from config_store import save_config
    from queue_helpers import get_pending

    save_config(_config())
    monkeypatch.setattr(discover_only, "WeChatAPI", lambda *args, **kwargs: _FirstAccountBlockedAPI())

    assert discover_only.main(["--format", "json", "--hours", "99999"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["meta"] == {
        "partial": False,
        "queued": 0,
        "completed_accounts": 0,
        "skipped_invalid": 0,
        "blocking_account": "First",
    }
    assert get_pending() == []


def test_discovery_reports_access_restriction_with_safe_details(monkeypatch, capsys):
    import discover_only
    from config_store import save_config

    config = _config()
    config["subscriptions"] = [{"name": "First", "biz": "biz-first"}]
    save_config(config)
    monkeypatch.setattr(discover_only, "WeChatAPI", lambda *args, **kwargs: _AccessRestrictedAPI())

    assert discover_only.main(["--format", "json", "--hours", "99999"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "WECHAT_ACCESS_RESTRICTED"
    assert payload["error"]["details"] == {
        "operation": "article_listing",
        "http_status": 403,
    }
    assert "cookie" not in json.dumps(payload)
    assert "token" not in json.dumps(payload)


def test_account_local_unresolved_identity_does_not_block_later_account(monkeypatch, capsys):
    import discover_only
    from config_store import save_config
    from queue_helpers import get_pending

    config = _config()
    config["subscriptions"][0].pop("biz")
    save_config(config)
    monkeypatch.setattr(discover_only, "WeChatAPI", lambda *args, **kwargs: _AccountLocalAPI())

    assert discover_only.main(["--format", "json", "--hours", "99999"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["status"] for item in payload["data"]["accounts"]] == ["unresolved", "ok"]
    assert [item["account"] for item in get_pending()] == ["Second"]


def test_discovery_persists_newly_resolved_account_identifier(monkeypatch):
    import discover_only
    from config_store import load_config, save_config

    config = _config()
    config["subscriptions"] = [{"name": "First"}]
    save_config(config)
    monkeypatch.setattr(discover_only, "WeChatAPI", lambda *args, **kwargs: _ResolvingAPI())

    assert discover_only.main(["--hours", "99999"]) == 0
    assert load_config()["subscriptions"][0]["biz"] == "resolved-biz"
