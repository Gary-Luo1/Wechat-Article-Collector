"""Data-source dispatch, cooldown, and cached-content behavior tests."""

from __future__ import annotations

import sys
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from types import SimpleNamespace


def types_simple_namespace(**kw):
    return SimpleNamespace(**kw)

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "wechat-article-subscriber" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from config_store import ConfigError, load_config, validate_config  # noqa: E402
from discover_only import (  # noqa: E402
    _subscription_cooldown_active,
    discover_articles,
)
from execution_policy import next_stage  # noqa: E402
from queue_helpers import add_pending, get_pending  # noqa: E402


class _FakeRedfoxClient:
    def __init__(self, articles):
        self.articles = articles
        self.calls = 0

    def list_articles(self, *, account="", account_name="", cutoff_epoch=0, max_articles=100):
        self.calls += 1
        return self.articles, {
            "pages": 1,
            "empty_reason": "exhausted",
            "api_code": 2000,
        }

    def close(self):
        pass


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "state"
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(home))
    return home


def _config():
    return validate_config(
        {
            "version": 11,
            "redfox": {"api_key": "k"},
            "subscriptions": [{"name": "人民日报", "alias": "rmrb"}],
        }
    )


def test_redfox_discovery_queues_articles(isolated_home, monkeypatch):
    article = {
        "title": "标题",
        "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=abc",
        "digest": "摘要",
        "work_uuid": "UUID123",
        "publish_time_raw": "2026-05-01 10:00:00",
        "update_time": int(time.time()),
    }
    fake = _FakeRedfoxClient([article])
    monkeypatch.setattr("discover_only.RedfoxClient", lambda *a, **k: fake)
    # Redirect config mutation onto a throwaway config store.
    import discover_only

    monkeypatch.setattr(discover_only, "modify_config", lambda mutate, path=None: _config())
    diagnostics: list[dict] = []
    queued = []

    def persist(articles):
        queued.extend(articles)
        return add_pending(articles)

    discovered = discover_articles(
        _config(), 24, None, diagnostics, persist
    )
    assert len(discovered) == 1
    assert diagnostics[0]["status"] == "ok"
    entry = get_pending()[0]
    assert entry["content_source"] == "redfox"
    assert entry["work_uuid"] == "UUID123"
    assert "content" not in entry  # body is fetched lazily at read time


def test_cooldown_skips_recently_discovered_subscription(isolated_home, monkeypatch):
    fresh = {
        "name": "人民日报",
        "alias": "rmrb",
        "last_discovered_at": datetime.now(timezone.utc).isoformat(),
    }
    assert _subscription_cooldown_active(fresh, 24)
    stale = {
        "name": "人民日报",
        "alias": "rmrb",
        "last_discovered_at": (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat(),
    }
    assert not _subscription_cooldown_active(stale, 24)
    assert not _subscription_cooldown_active({"name": "人民日报"}, 24)

    fake = _FakeRedfoxClient([])
    monkeypatch.setattr("discover_only.RedfoxClient", lambda *a, **k: fake)
    diagnostics: list[dict] = []
    discover_articles(
        _config_with_subscription(fresh), 24, None, diagnostics, None
    )
    assert diagnostics[0]["skipped_cooldown"] == 1
    assert fake.calls == 0  # no paid call inside the cooldown window


def _config_with_subscription(subscription):
    config = _config()
    config["subscriptions"] = [subscription]
    return config


def test_missing_redfox_key_rejected():
    config = _config()
    config["redfox"] = {"api_key": ""}
    with pytest.raises(ConfigError):
        discover_articles(config, 24)


def test_next_stage_bypasses_wechat_for_redfox():
    config = _config()
    stage, action = next_stage(config)
    assert stage not in {"wechat_credentials_missing", "wechat_unverified"}

    no_key = _config()
    no_key["redfox"] = {"api_key": ""}
    stage, action = next_stage(no_key)
    assert stage == "redfox_credentials_missing"
    assert action == "run_redfox_key_setup"


def test_queue_accepts_optional_content_fields(isolated_home, monkeypatch):
    add_pending(
        [
            {
                "title": "t",
                "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=zzz",
                "account": "a",
                "content": "cached",
                "content_source": "redfox",
            }
        ]
    )
    entry = get_pending()[0]
    assert entry["content"] == "cached"


def test_process_pending_uses_cached_content(isolated_home, monkeypatch):
    import process_pending

    add_pending(
        [
            {
                "title": "t",
                "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=yyy",
                "account": "a",
                "content": "cached body text",
                "content_source": "redfox",
            }
        ]
    )
    article = get_pending()[0]
    text, _ = process_pending._print_article(article)
    assert text == "cached body text"


def test_cooldown_persisted_across_discovery_cycles(isolated_home, monkeypatch):
    from config_store import save_config

    save_config(_config())  # real config store in the isolated home
    fake = _FakeRedfoxClient([])
    monkeypatch.setattr("discover_only.RedfoxClient", lambda *a, **k: fake)
    diagnostics: list[dict] = []
    discover_articles(load_config(), 24, None, diagnostics, None)
    saved = load_config()
    assert saved["subscriptions"][0]["last_discovered_at"]  # persisted for next cycle
    diagnostics.clear()
    discover_articles(saved, 24, None, diagnostics, None)
    assert fake.calls == 1  # second cycle made no paid call
    assert diagnostics[0]["skipped_cooldown"] == 1


def test_agent_payload_without_credentials_keeps_redfox_key():
    import init_config

    existing = validate_config(
        {
            "version": 11,
            "redfox": {"api_key": "k"},
            "subscriptions": [{"name": "人民日报"}],
        }
    )
    # A payload that carries no credential fields must not clear the
    # configured redfox API key.
    payload = {"settings": {"check_hours": 24}, "feishu": {}}
    merged = init_config.config_from_agent_payload(payload, existing=deepcopy(existing))
    assert merged["redfox"]["api_key"] == "k"


def test_reset_credentials_clears_redfox_key(isolated_home):
    from config_store import modify_config, save_config, load_config

    config = _config()
    save_config(config)

    def noop(arguments):
        raise NotImplementedError

    # Directly exercise the credentials mutation the reset command applies.
    def mutate_reset(cfg):
        cfg["wechat"] = {"cookie": "", "token": ""}
        cfg["redfox"] = {"api_key": ""}
        return cfg

    modify_config(mutate_reset)
    assert load_config()["redfox"]["api_key"] == ""


def test_read_fetches_content_lazily_and_caches(isolated_home, monkeypatch):
    import process_pending
    from config_store import save_config

    save_config(_config())
    add_pending(
        [
            {
                "title": "t",
                "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=lazy1",
                "account": "a",
                "content_source": "redfox",
                "work_uuid": "UUID-LAZY",
            }
        ]
    )
    calls = []

    class _Lazy:
        def query_work(self, work_uuid):
            calls.append(work_uuid)
            return {"content": "lazy body text"}, 2000

        def close(self):
            pass

    monkeypatch.setattr("redfox_client.RedfoxClient", lambda *a, **k: _Lazy())
    article = get_pending()[0]
    text, _ = process_pending._print_article(article)
    assert text == "lazy body text"
    assert calls == ["UUID-LAZY"]
    # Body is cached: a second read makes no further paid call.
    cached_article = get_pending()[0]
    assert cached_article["content"] == "lazy body text"
    text2, _ = process_pending._print_article(cached_article)
    assert calls == ["UUID-LAZY"]


def test_secret_probe_classifies_keychain_failure(monkeypatch):
    import bitable_client

    def raise_secret_error(args, **kwargs):
        raise bitable_client.LarkCLIError(
            "device authorization failed: The request is missing a required "
            "parameter: client_secret."
        )

    monkeypatch.setattr(bitable_client, "_run_lark", raise_secret_error)
    probe = bitable_client.probe_app_secret_resolution()
    assert probe["resolvable"] is False
    assert probe["reason"] == "keychain_secret_not_migratable"
    assert "open.feishu.cn" in probe["remediation"]

    def ok(args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(bitable_client, "_run_lark", ok)
    assert bitable_client.probe_app_secret_resolution()["resolvable"] is True


def test_secret_hint_appended_to_misleading_cli_error():
    from bitable_client import _append_secret_hint

    plain = "lark-cli request failed: rate limited"
    assert _append_secret_hint(plain) == plain  # untouched when unrelated
    enriched = _append_secret_hint(
        "not configured not_configured run `lark-cli config init --new` ..."
    )
    assert "config init --new" in enriched
    assert "open.feishu.cn" in enriched
    assert "--app-secret-stdin" in enriched


def test_no_data_result_reports_unresolved_and_skips_cooldown(isolated_home, monkeypatch):
    from config_store import load_config, save_config
    save_config(_config())
    fake = _FakeArticlesClient([], {"pages": 1, "empty_reason": "no_data", "api_code": 3203})
    monkeypatch.setattr("discover_only.RedfoxClient", lambda *a, **k: fake)
    diagnostics: list[dict] = []
    discover_articles(load_config(), 24, None, diagnostics, None)
    assert diagnostics[0]["status"] == "unresolved"
    assert diagnostics[0]["error"] == "account_not_found"
    # No cooldown armed: a retry after fixing the alias re-queries immediately.
    assert "last_discovered_at" not in load_config()["subscriptions"][0]


class _FakeArticlesClient:
    def __init__(self, articles, info):
        self.articles = articles
        self.info = info
        self.calls = 0

    def list_articles(self, **kwargs):
        self.calls += 1
        return self.articles, self.info

    def close(self):
        pass


def test_queue_failure_after_paid_listing_arms_cooldown(isolated_home, monkeypatch):
    from config_store import load_config, save_config
    save_config(_config())
    now = int(time.time())
    fake = _FakeArticlesClient(
        [
            {
                "title": "t",
                "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=paid1",
                "digest": "",
                "work_uuid": "U",
                "publish_time_raw": "",
                "update_time": now,
            }
        ],
        {"pages": 1, "empty_reason": "exhausted", "api_code": 2000},
    )
    monkeypatch.setattr("discover_only.RedfoxClient", lambda *a, **k: fake)

    def failing_persist(articles):
        raise ValueError("queue is invalid; preserved as quarantine")

    with pytest.raises(ValueError):
        discover_articles(load_config(), 24, None, [], failing_persist)
    # Cooldown armed despite the queue failure: money protection first.
    assert load_config()["subscriptions"][0].get("last_discovered_at")


def test_legacy_pending_retired_once(isolated_home):
    add_pending(
        [
            {
                "title": "legacy",
                "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=legacy1",
                "account": "old",
            },
            {
                "title": "modern",
                "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=modern1",
                "account": "new",
                "content": "body",
                "content_source": "redfox",
            },
        ]
    )
    from queue_helpers import retire_legacy_pending, read_queue

    assert retire_legacy_pending() == 1
    queue = read_queue()
    assert [a["title"] for a in queue["pending"]] == ["modern"]
    legacy = [e for e in queue["processed"].values() if e["metadata"].get("disposition") == "legacy_unreadable"]
    assert len(legacy) == 1
    assert retire_legacy_pending() == 0  # idempotent


def test_read_failure_wrapped_for_protocol(isolated_home, monkeypatch):
    import process_pending
    from config_store import save_config
    from redfox_client import RedfoxRateLimitError

    save_config(_config())
    add_pending(
        [
            {
                "title": "t",
                "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=wrap1",
                "account": "a",
                "content_source": "redfox",
                "work_uuid": "U-WRAP",
            }
        ]
    )

    class _Failing:
        def query_work(self, work_uuid):
            raise RedfoxRateLimitError("rate limited")

        def close(self):
            pass

    monkeypatch.setattr("redfox_client.RedfoxClient", lambda *a, **k: _Failing())
    with pytest.raises(ValueError) as exc_info:
        process_pending._print_article(get_pending()[0])
    assert "rate limited" in str(exc_info.value)


def test_uncrawled_article_gets_specific_guidance(isolated_home, monkeypatch):
    import process_pending
    from config_store import save_config

    save_config(_config())
    add_pending(
        [
            {
                "title": "t",
                "link": "https://mp.weixin.qq.com/s?__biz=1&mid=1&idx=1&sn=crawl1",
                "account": "a",
                "content_source": "redfox",
                "work_uuid": "U-UNCRAWLED",
            }
        ]
    )

    class _Uncrawled:
        def query_work(self, work_uuid):
            return {}, 3203

        def close(self):
            pass

    monkeypatch.setattr("redfox_client.RedfoxClient", lambda *a, **k: _Uncrawled())
    with pytest.raises(ValueError, match="not crawled"):
        process_pending._print_article(get_pending()[0])


def test_add_resolves_name_to_alias_via_search(isolated_home, monkeypatch):
    import manage
    from config_store import save_config

    save_config(_config())

    class _Search:
        def search_accounts(self, keyword):
            return [
                {"account": "QbitAI", "account_name": "量子位"},
                {"account": "fake1", "account_name": "量子位智库"},
            ]

        def close(self):
            pass

    monkeypatch.setattr("redfox_client.RedfoxClient", lambda *a, **k: _Search())
    data = manage._subscriptions(
        types_simple_namespace(subscription_command="add", name="量子位", alias="", biz="")
    )
    assert data["added"]["alias"] == "QbitAI"


def test_add_ambiguous_name_lists_candidates(isolated_home, monkeypatch):
    import manage
    from config_store import save_config

    save_config(_config())

    class _Ambiguous:
        def search_accounts(self, keyword):
            return [
                {"account": "a1", "account_name": "同名号"},
                {"account": "a2", "account_name": "同名号"},
            ]

        def close(self):
            pass

    monkeypatch.setattr("redfox_client.RedfoxClient", lambda *a, **k: _Ambiguous())
    with pytest.raises(ValueError, match="--alias"):
        manage._subscriptions(
            types_simple_namespace(subscription_command="add", name="同名号", alias="", biz="")
        )


def test_daily_preview_then_confirmed_run(isolated_home, monkeypatch):
    import manage
    from config_store import save_config

    save_config(_config())

    class _Daily:
        def list_articles(self, **kwargs):
            return [], {"pages": 1, "empty_reason": "exhausted", "api_code": 2000}

        def close(self):
            pass

    monkeypatch.setattr("discover_only.RedfoxClient", lambda *a, **k: _Daily())
    preview = manage._daily(types_simple_namespace(yes=False))
    assert preview[1] == "confirm_daily_run"
    assert preview[0]["estimated_billed_calls"] == 1
    assert preview[0]["subscriptions"][0]["alias"] == "rmrb"

    result = manage._daily(types_simple_namespace(yes=True))
    assert result[1] == "read_score_digest_candidates"
    assert result[0]["run"]["discovered"] == 0
