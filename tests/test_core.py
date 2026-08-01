from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "wechat-article-subscriber" / "scripts"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))


def article(letter: str, *, query: str = "") -> dict:
    suffix = f"?__biz=b&mid={letter}&sn={letter}{query}" if query else f"/{letter}"
    return {
        "title": f"Article {letter}",
        "link": f"https://mp.weixin.qq.com/s{suffix}",
        "digest": f"Digest {letter}",
        "account": "Example",
        "update_time": 1_700_000_000,
    }


def test_feishu_target_owns_cli_check_preflight_and_sync_calls():
    from feishu_target import FeishuTarget

    calls: list[tuple] = []
    target = FeishuTarget(
        {"enabled": True, "identity": "bot"},
        cli_info=lambda: {"compatible": True, "version": "1.0.69"},
        preflight=lambda feishu: calls.append(("preflight", feishu)) or {"mapping": {}},
        upsert=lambda feishu, article, metadata, dry_run: calls.append(
            ("upsert", feishu, article, metadata, dry_run)
        ),
    )

    assert target.check() == {"mapping": {}}
    target.sync({"title": "Article"}, {"score": 8.0}, dry_run=True)
    assert [call[0] for call in calls] == ["preflight", "upsert"]


class TestConfig:
    def test_save_load_and_defaults(self):
        from config_store import load_config, save_config

        config = {
            "wechat": {"cookie": "secret", "token": "123"},
            "subscriptions": [{"name": "Example"}],
            "feishu": {"base_token": "", "table_id": ""},
            "settings": {
                "check_hours": 24,
                "request_delay": 0,
                "max_articles_per_account": 10,
                "content_dedup": True,
                "min_score": 6,
            },
        }
        path = save_config(config)
        assert path.exists()
        assert load_config(require_wechat=True)["wechat"]["token"] == "123"

    def test_rejects_bad_range(self):
        from config_store import DEFAULT_CONFIG, ConfigError, validate_config

        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["settings"]["min_score"] = 100
        with pytest.raises(ConfigError):
            validate_config(config)

    def test_agent_dialogue_payload_saves_without_echoing_secrets(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        import init_config
        from config_store import load_config

        payload = {
            "wechat_cookie": "sensitive-cookie-value",
            "wechat_token": "sensitive-token-value",
            "subscriptions": ["Account One", {"name": "Account Two", "alias": "two"}],
            "feishu_base_token": "base-token",
            "feishu_table_id": "table-id",
        }
        monkeypatch.setattr(init_config.sys, "stdin", io.StringIO(json.dumps(payload)))

        assert init_config.main(["--agent-stdin"]) == 0
        captured = capsys.readouterr()
        assert "sensitive-cookie-value" not in captured.out + captured.err
        assert "sensitive-token-value" not in captured.out + captured.err
        config = load_config(require_wechat=True)
        assert config["wechat"]["cookie"] == "sensitive-cookie-value"
        assert [item["name"] for item in config["subscriptions"]] == [
            "Account One",
            "Account Two",
        ]
        assert config["feishu"]["enabled"] is True
        assert config["feishu"]["identity"] == "user"
        assert config["feishu"]["base_token"] == "base-token"
        assert config["feishu"]["field_mapping"]["url"]["name"] == "文章链接"

    def test_feishu_only_dialogue_merges_without_wechat_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import init_config
        from config_store import DEFAULT_CONFIG, load_config, save_config

        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["wechat"] = {"cookie": "keep-cookie", "token": "123"}
        config["subscriptions"] = [{"name": "Account"}]
        save_config(config)
        payload = {
            "enabled": True,
            "identity": "user",
            "expected_app_id": "cli_expected",
            "base_token": "base",
            "table_id": "tbl1",
            "provisioning": "existing",
            "schema_policy": "mapped",
            "field_mapping": {
                "title": {"field_id": "fld_title", "name": "标题"},
                "url": {"field_id": "fld_url", "name": "链接"},
            },
        }
        monkeypatch.setattr(init_config.sys, "stdin", io.StringIO(json.dumps(payload)))

        assert init_config.main(["--feishu-agent-stdin"]) == 0
        saved = load_config(require_wechat=True)
        assert saved["wechat"]["cookie"] == "keep-cookie"
        assert saved["feishu"]["expected_app_id"] == "cli_expected"
        assert saved["feishu"]["field_mapping"]["title"]["field_id"] == "fld_title"

    def test_agent_dialogue_payload_rejects_partial_feishu_config(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        import init_config
        from paths import config_path

        payload = {
            "wechat_cookie": "cookie",
            "wechat_token": "token",
            "subscriptions": ["Account"],
            "feishu_base_token": "base-only",
            "feishu_table_id": "",
        }
        monkeypatch.setattr(init_config.sys, "stdin", io.StringIO(json.dumps(payload)))

        assert init_config.main(["--agent-stdin"]) == 1
        assert not config_path().exists()
        captured = capsys.readouterr()
        assert "base-only" not in captured.out + captured.err

    def test_runtime_forwards_agent_configuration_on_stdin(self):
        from config_store import load_config

        payload = {
            "wechat_cookie": "runtime-cookie-secret",
            "wechat_token": "runtime-token-secret",
            "subscriptions": ["Runtime Account"],
            "feishu_base_token": "",
            "feishu_table_id": "",
        }
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "runtime.py"), "setup", "--agent-stdin"],
            input=json.dumps(payload),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=os.environ.copy(),
        )

        assert result.returncode == 0, result.stderr
        assert "runtime-cookie-secret" not in result.stdout + result.stderr
        assert "runtime-token-secret" not in result.stdout + result.stderr
        assert load_config(require_wechat=True)["subscriptions"][0]["name"] == "Runtime Account"

    def test_agent_file_fallback_is_scoped_consumed_and_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ):
        import init_config
        from config_store import load_config
        from paths import data_dir

        assert init_config.main(["--prepare-agent-file"]) == 0
        inbox = Path(capsys.readouterr().out.strip())
        assert inbox.parent == data_dir()
        payload = {
            "wechat_cookie": "inbox-cookie-secret",
            "wechat_token": "inbox-token-secret",
            "subscriptions": ["Inbox Account"],
            "feishu_base_token": "",
            "feishu_table_id": "",
        }
        inbox.write_text(json.dumps(payload), encoding="utf-8")

        assert init_config.main(["--agent-file", str(inbox)]) == 0
        captured = capsys.readouterr()
        assert "inbox-cookie-secret" not in captured.out + captured.err
        assert "inbox-token-secret" not in captured.out + captured.err
        assert not inbox.exists()
        assert load_config(require_wechat=True)["subscriptions"][0]["name"] == "Inbox Account"

    def test_agent_file_fallback_rejects_and_preserves_unscoped_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        import init_config

        outside = tmp_path / ".agent-config-outside.json"
        outside.write_text('{"secret":"must-not-be-read"}', encoding="utf-8")

        assert init_config.main(["--agent-file", str(outside)]) == 1
        assert outside.exists()
        captured = capsys.readouterr()
        assert "must-not-be-read" not in captured.out + captured.err


class TestQueue:
    def test_normalize_wechat_tracking(self):
        from queue_helpers import normalize_url

        first = normalize_url(
            "https://mp.weixin.qq.com/s?__biz=x&mid=1&sn=a&scene=1"
        )
        second = normalize_url(
            "https://mp.weixin.qq.com/s?sn=a&mid=1&__biz=x&chksm=ignored"
        )
        assert first == second

    def test_add_deduplicates_normalized_url(self):
        from queue_helpers import add_pending, get_pending

        first = article("1", query="&scene=1")
        second = article("1", query="&scene=2")
        assert add_pending([first, second]) == 1
        assert len(get_pending()) == 1

    def test_content_dedup_is_off_by_default(self):
        from queue_helpers import add_pending, get_pending

        first = article("a")
        second = {**first, "link": "https://mp.weixin.qq.com/s/b"}
        assert add_pending([first, second]) == 2
        assert len(get_pending()) == 2

    def test_content_dedup_can_be_enabled_explicitly(self):
        from queue_helpers import add_pending, get_pending

        first = article("a")
        second = {**first, "link": "https://mp.weixin.qq.com/s/b"}
        assert add_pending([first, second], content_dedup=True) == 1
        assert len(get_pending()) == 1

    def test_complete_by_stable_link(self):
        from queue_helpers import add_pending, complete_article, read_queue

        add_pending([article("a"), article("b")])
        entry = complete_article(article("a")["link"], {"score": 8})
        queue = read_queue()
        assert entry["article"]["title"] == "Article a"
        assert [item["title"] for item in queue["pending"]] == ["Article b"]
        assert next(iter(queue["processed"].values()))["metadata"]["score"] == 8

    def test_pending_sync_survives_cleanup(self):
        from queue_helpers import add_pending, cleanup_processed, complete_article, pending_sync_entries

        add_pending([article("a")])
        complete_article(article("a")["link"], {"score": 8}, sync_status="pending")
        assert cleanup_processed(1) == 0
        assert len(pending_sync_entries()) == 1

    def test_inbox_metadata_is_reversible(self):
        from queue_helpers import add_pending, read_queue, update_inbox_item

        add_pending([article("a")])
        updated = update_inbox_item(
            article("a")["link"], favorite=True, state="later"
        )
        assert updated["favorite"] is True
        assert updated["inbox_state"] == "later"
        update_inbox_item(article("a")["link"], favorite=False, state="active")
        saved = read_queue()["pending"][0]
        assert saved["favorite"] is False
        assert saved["inbox_state"] == "active"

    def test_dismiss_and_restore_preserve_stable_identity(self):
        from queue_helpers import add_pending, dismiss_article, read_queue, restore_dismissed

        add_pending([article("a")])
        dismissed = dismiss_article(article("a")["link"])
        assert dismissed["metadata"]["disposition"] == "dismissed"
        assert not read_queue()["pending"]
        restored = restore_dismissed(article("a")["link"])
        assert restored["normalized_url"] == dismissed["article"]["normalized_url"]
        queue = read_queue()
        assert len(queue["pending"]) == 1
        assert queue["processed"] == {}

    def test_complete_after_dismiss_raises(self):
        from queue_helpers import (
            add_pending,
            complete_article,
            dismiss_article,
            read_queue,
        )

        add_pending([article("a")])
        dismissed = dismiss_article(article("a")["link"])
        with pytest.raises(LookupError, match="dismissed"):
            complete_article(article("a")["link"], {"score": 8}, sync_status="pending")
        queue = read_queue()
        entry = next(iter(queue["processed"].values()))
        assert entry["metadata"] == dismissed["metadata"]
        assert entry["sync_status"] == "not_requested"

    def test_corruption_is_quarantined(self):
        from paths import queue_path
        from queue_helpers import read_queue

        queue_path().parent.mkdir(parents=True)
        queue_path().write_text("{broken", encoding="utf-8")
        with pytest.raises(ValueError, match="preserved"):
            read_queue()
        assert list(queue_path().parent.glob("queue.corrupt.*.json"))

    def test_structural_corruption_cli_fails_without_traceback(self):
        from paths import queue_path

        queue_path().parent.mkdir(parents=True)
        queue_path().write_text(
            json.dumps({"version": 1, "pending": ["attacker-string"], "processed": {}}),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "process_pending.py"), "list"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=os.environ.copy(),
        )
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert "queue is invalid" in result.stderr
        assert list(queue_path().parent.glob("queue.corrupt.*.json"))


def test_article_inbox_query_returns_local_state_without_cli_arguments():
    from queue_helpers import add_pending, complete_article

    add_pending([article("pending"), article("processed")])
    complete_article(article("processed")["link"], {"score": 8}, sync_status="pending")

    from article_inbox import query_inbox

    result = query_inbox(status="all", sort="oldest", limit=10)

    assert result["summary"]["pending"] == 1
    assert result["summary"]["processed"] == 1
    assert result["summary"]["sync_pending"] == 1
    assert [item["status"] for item in result["items"]] == ["pending", "processed"]


class TestScoring:
    def scores(self):
        return {
            "技术深度": 8,
            "信息新颖度": 7,
            "分析深度与独立观点": 9,
            "实用参考价值": 6,
            "内容质量与可信度": 8,
        }

    def test_calculate_exact_five_dimensions(self):
        from scoring_rubric import calculate_score

        assert calculate_score(self.scores()) == 7.8

    def test_missing_dimension_rejected(self):
        from scoring_rubric import calculate_score

        values = self.scores()
        values.pop("技术深度")
        with pytest.raises(ValueError, match="five dimensions"):
            calculate_score(values)

    def test_out_of_range_rejected(self):
        from scoring_rubric import validate_total_score

        with pytest.raises(ValueError):
            validate_total_score(11)

    def test_ad_heuristic_uses_disclosure_not_generic_word(self):
        from scoring_rubric import is_advertisement

        assert is_advertisement("推广 | 新产品")
        assert is_advertisement("普通标题", "本文为广告，感谢支持")
        assert not is_advertisement("广告行业研究", "讨论广告行业的技术变化")


class TestReader:
    def test_url_allowlist(self):
        from article_reader import is_wechat_article

        assert is_wechat_article("https://mp.weixin.qq.com/s/abc")
        assert is_wechat_article("https://mp.weixin.qq.com/s?__biz=x")
        assert not is_wechat_article("http://mp.weixin.qq.com/s/abc")
        assert not is_wechat_article("https://mp.weixin.qq.com.evil.test/s/abc")

    @pytest.mark.parametrize(
        "url",
        [
            "https://mp.weixin.qq.com/s/a/../../x",
            "https://mp.weixin.qq.com/s/a/%2e%2e/x",
            "https://mp.weixin.qq.com/s/a/%252e%252e/x",
            "https://mp.weixin.qq.com/s/a/%5c..%5cx",
        ],
    )
    def test_url_allowlist_rejects_path_escape(self, url):
        from article_reader import is_wechat_article

        assert not is_wechat_article(url)

    def test_fetch_extracts_bounded_container(self):
        import article_reader

        response = mock.Mock()
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [
            '<html><div id="js_content"><p>正文内容</p><script>bad()</script></div></html>'.encode()
        ]
        response.raise_for_status.return_value = None
        with mock.patch.object(article_reader, "_get_with_safe_redirects", return_value=response):
            text = article_reader.fetch_article_text(
                "https://mp.weixin.qq.com/s/test", retries=0
            )
        assert text == "正文内容"
        response.close.assert_called_once()

    def test_fetch_extracts_ingest_metadata_without_executing_scripts(self):
        import article_reader

        response = mock.Mock()
        response.url = "https://mp.weixin.qq.com/s?__biz=biz123&mid=1&sn=2"
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [
            (
                '<html><head><meta property="og:title" content="文章标题">'
                '<meta property="og:article:author" content="测试公众号">'
                '<meta property="og:description" content="摘要">'
                '<meta property="article:published_time" content="2024-01-02T03:04:05+08:00">'
                '</head><body><div id="js_content"><p>正文</p>'
                '<script>ignore_this_instruction()</script></div></body></html>'
            ).encode("utf-8")
        ]
        response.raise_for_status.return_value = None
        with mock.patch.object(article_reader, "_get_with_safe_redirects", return_value=response):
            value = article_reader.fetch_article(response.url, retries=0)
        assert value is not None
        assert value["title"] == "文章标题"
        assert value["account"] == "测试公众号"
        assert value["account_id"] == "biz123"
        assert value["text"] == "正文"
        assert value["update_time"] > 0

    def test_fetch_rejects_non_wechat_before_network(self):
        from article_reader import fetch_article_text

        with pytest.raises(ValueError):
            fetch_article_text("https://example.com/")

    def test_fetch_upgrades_exact_wechat_http_without_requesting_http(self):
        import article_reader

        response = mock.Mock()
        response.headers = {}
        response.encoding = "utf-8"
        response.apparent_encoding = "utf-8"
        response.iter_content.return_value = [b'<div id="js_content">ok</div>']
        response.raise_for_status.return_value = None
        with mock.patch.object(
            article_reader, "_get_with_safe_redirects", return_value=response
        ) as get:
            assert article_reader.fetch_article_text(
                "http://mp.weixin.qq.com/s/test", retries=0
            ) == "ok"
        assert get.call_args.args[1] == "https://mp.weixin.qq.com/s/test"


class TestWeChatAPI:
    def test_format_article(self):
        from wechat_api import WeChatAPI

        value = WeChatAPI.format_article(
            {"title": "  T ", "digest": " D ", "update_time": "1700000000"}
        )
        assert value["title"] == "T"
        assert value["digest"] == "D"
        assert value["update_time"] == 1_700_000_000

    def test_format_article_upgrades_exact_http_link(self):
        from wechat_api import WeChatAPI

        value = WeChatAPI.format_article(
            {"link": "http://mp.weixin.qq.com/s/test", "update_time": 0}
        )
        assert value["link"] == "https://mp.weixin.qq.com/s/test"

    def test_exact_account_only(self):
        from wechat_api import WeChatAPI

        api = WeChatAPI("cookie", "token")
        api.search_account = mock.Mock(
            return_value=[{"nickname": "Similar", "alias": "other"}]
        )
        assert api.get_account(name="Wanted") is None

    def test_expired_token_mapping(self):
        from wechat_api import WeChatAPI, WeChatTokenExpired

        with pytest.raises(WeChatTokenExpired):
            WeChatAPI._raise_api_error(
                {"base_resp": {"ret": 200003, "err_msg": "expired"}}, "test"
            )

    def test_invalid_args_distinguishes_incomplete_cookie(self):
        from wechat_api import WeChatAPI, WeChatCredentialContextError

        with pytest.raises(WeChatCredentialContextError, match="incomplete"):
            WeChatAPI._raise_api_error(
                {"base_resp": {"ret": -2, "err_msg": "invalid args"}},
                "test",
                {"sessionid"},
            )


class TestBitable:
    def test_field_schema_uses_number_for_score(self):
        from bitable_client import REQUIRED_FIELDS

        assert REQUIRED_FIELDS["AI评分"]["numeric_type"] == 2

    def test_record_uses_human_datetime_and_ai_summary(self):
        from bitable_client import build_record

        record = build_record(article("a"), {"score": 8, "summary": "AI summary", "tags": ["AI"]})
        assert record["AI评分"] == 8.0
        assert record["文章摘要"] == "AI summary"
        assert record["发布日期"].startswith("2023-")

    def test_standard_url_schema_uses_text_url_style(self):
        import bitable_client

        url_field = next(
            field for field in bitable_client.standard_field_schema() if field["name"] == "文章链接"
        )
        assert url_field["type"] == "text"
        assert url_field["style"] == {"type": "url"}

    def test_missing_required_field_does_not_auto_create(self):
        import bitable_client

        with pytest.raises(bitable_client.LarkCLIError, match="no compatible"):
            bitable_client.resolve_field_mapping(
                [{"field_id": "fld_title", "name": "文章标题", "type": "text"}], {}
            )

    def test_existing_fields_are_mapped_by_alias_type_and_id(self):
        import bitable_client

        mapping = bitable_client.resolve_field_mapping(
            [
                {"field_id": "fld_title", "name": "标题", "type": "text"},
                {"field_id": "fld_url", "name": "原文链接", "type": 15},
                {"field_id": "fld_account", "name": "公众号", "type": "text"},
                {"field_id": "fld_formula", "name": "评分", "type": "formula"},
            ],
            {},
        )
        assert mapping["title"]["field_id"] == "fld_title"
        assert mapping["url"]["field_id"] == "fld_url"
        assert mapping["account"]["field_id"] == "fld_account"
        assert "score" not in mapping

    def test_one_field_cannot_back_two_logical_mappings(self):
        import bitable_client

        fields = [{"field_id": "fld_one", "name": "内容", "type": "text"}]
        configured = {
            "title": {"field_id": "fld_one"},
            "url": {"field_id": "fld_one"},
        }
        with pytest.raises(bitable_client.LarkCLIError, match="mapped to both"):
            bitable_client.resolve_field_mapping(fields, configured)

    def test_preflight_rejects_wrong_app_profile(self):
        import bitable_client

        feishu = {
            "enabled": True,
            "identity": "user",
            "expected_app_id": "cli_expected",
            "base_token": "base",
            "table_id": "table",
            "field_mapping": {},
        }
        auth = {
            "appId": "cli_other",
            "identities": {
                "user": {"available": True, "status": "ready", "tokenStatus": "valid"}
            },
        }
        with mock.patch.object(bitable_client, "_run_lark", return_value=auth), pytest.raises(
            bitable_client.LarkCLIError
        ) as error:
            bitable_client.preflight_feishu(feishu)
        assert error.value.kind == "wrong_app"

    def test_preflight_requires_explicit_expected_app_id(self):
        import bitable_client

        feishu = {
            "enabled": True,
            "identity": "user",
            "expected_app_id": "",
            "base_token": "base",
            "table_id": "table",
            "field_mapping": {},
        }
        auth = {
            "appId": "cli_actual",
            "identities": {
                "user": {"available": True, "status": "ready", "tokenStatus": "valid"}
            },
        }
        with mock.patch.object(bitable_client, "_run_lark", return_value=auth), pytest.raises(
            bitable_client.LarkCLIError
        ) as error:
            bitable_client.preflight_feishu(feishu)
        assert error.value.kind == "wrong_app"
        assert "explicitly confirmed" in str(error.value)

    def test_preflight_rejects_different_authorized_user(self):
        import bitable_client

        feishu = {
            "enabled": True,
            "identity": "user",
            "expected_app_id": "cli_expected",
            "expected_user_open_id": "ou_expected",
            "base_token": "base",
            "table_id": "table",
            "field_mapping": {},
        }
        auth = {
            "appId": "cli_expected",
            "identities": {
                "user": {
                    "available": True,
                    "status": "ready",
                    "tokenStatus": "valid",
                    "openId": "ou_other",
                }
            },
        }
        with mock.patch.object(bitable_client, "_run_lark", return_value=auth), pytest.raises(
            bitable_client.LarkCLIError
        ) as error:
            bitable_client.preflight_feishu(feishu)
        assert error.value.kind == "wrong_app"
        assert "authorized Feishu user" in str(error.value)

    def test_feishu_identity_context_is_redacted(self):
        import bitable_client

        responses = [
            {
                "data": {
                    "appId": "cli_expected",
                    "appSecret": "must-not-leak",
                    "profile": "default",
                }
            },
            {
                "data": {
                    "appId": "cli_expected",
                    "identity": "user",
                    "identities": {
                        "user": {
                            "available": True,
                            "status": "ready",
                            "tokenStatus": "valid",
                            "userName": "Example User",
                            "openId": "ou_expected",
                        }
                    },
                }
            },
        ]
        with mock.patch.object(bitable_client, "_run_lark", side_effect=responses):
            context = bitable_client.feishu_identity_context(verify=True)
        assert context["app_id"] == "cli_expected"
        assert context["user"]["open_id"] == "ou_expected"
        assert "must-not-leak" not in json.dumps(context)

    def test_profile_list_json_array_is_supported(self):
        import bitable_client

        profiles = bitable_client._json_value(
            '[{"name":"other","appId":"cli_other","active":true}]'
        )
        assert isinstance(profiles, list)
        assert profiles[0]["appId"] == "cli_other"

    def test_profile_resolution_uses_conversation_app_not_active_default(self):
        import bitable_client

        profiles = [
            {"name": "default-other", "appId": "cli_other", "active": True},
            {"name": "current-bot", "appId": "cli_current", "active": False},
        ]
        with mock.patch.object(
            bitable_client, "_run_lark", return_value=profiles
        ) as run:
            resolved = bitable_client.resolve_lark_profile("cli_current")
        assert resolved["profile"] == "current-bot"
        assert resolved["matched_by"] == "current_conversation_app_id"
        assert resolved["default_profile_ignored"] is True
        assert run.call_args.args[0] == ["profile", "list"]

    @pytest.mark.parametrize(
        "profiles, message",
        [
            (
                [{"name": "other", "appId": "cli_other", "active": True}],
                "no lark-cli profile matches",
            ),
            (
                [
                    {"name": "first", "appId": "cli_current", "active": False},
                    {"name": "second", "appId": "cli_current", "active": False},
                ],
                "multiple lark-cli profiles match",
            ),
        ],
    )
    def test_profile_resolution_stops_when_match_is_not_unique(
        self, profiles, message
    ):
        import bitable_client

        with mock.patch.object(bitable_client, "_run_lark", return_value=profiles):
            with pytest.raises(bitable_client.LarkCLIError, match=message) as error:
                bitable_client.resolve_lark_profile("cli_current")
        assert error.value.kind == "wrong_app"

    def test_agent_preflight_does_not_read_auth_through_a_stale_profile(self):
        import bitable_client

        feishu = {
            "identity": "bot",
            "binding_mode": "agent",
            "expected_app_id": "cli_current",
            "cli_profile": "stale-other",
        }

        def run_lark(args, *, retries=3):
            assert args == ["profile", "list"]
            return [
                {
                    "name": "current-bot",
                    "appId": "cli_current",
                    "active": False,
                }
            ]

        with mock.patch.object(bitable_client, "_run_lark", side_effect=run_lark):
            with pytest.raises(
                bitable_client.LarkCLIError,
                match="locally pinned lark-cli profile",
            ):
                bitable_client.verify_feishu_identity(feishu)

    def test_upsert_uses_current_command(self):
        import bitable_client

        mapping = {
            "title": {
                "field_id": "fld_title",
                "name": "标题",
                "type": "text",
                "raw": {"field_id": "fld_title", "name": "标题", "type": "text"},
            },
            "url": {
                "field_id": "fld_url",
                "name": "链接",
                "type": "text",
                "raw": {"field_id": "fld_url", "name": "链接", "type": "text"},
            },
        }
        feishu = {
            "enabled": True,
            "identity": "user",
            "base_token": "base",
            "table_id": "table",
        }
        with mock.patch.object(
            bitable_client,
            "preflight_feishu",
            return_value={"identity": "user", "resolved": mapping},
        ), mock.patch.object(
            bitable_client, "find_record_by_url", return_value="rec1"
        ), mock.patch.object(bitable_client, "_run_lark", return_value={"ok": True}) as run:
            bitable_client.upsert_article(feishu, article("a"), {"score": 8})
        args = run.call_args.args[0]
        assert "+record-upsert" in args
        assert args[args.index("--as") + 1] == "user"
        assert args[args.index("--record-id") + 1] == "rec1"

    def test_grant_bot_created_resource_uses_full_access_once(self):
        import bitable_client

        with mock.patch.object(
            bitable_client, "_run_lark", return_value={"ok": True}
        ) as run:
            bitable_client.grant_bot_created_resource(
                "base_token", "bitable", "ou_manager"
            )
        args = run.call_args.args[0]
        assert args[:2] == ["drive", "+member-add"]
        assert args[args.index("--token") + 1] == "base_token"
        assert args[args.index("--member-id") + 1] == "ou_manager"
        assert args[args.index("--member-type") + 1] == "openid"
        assert args[args.index("--perm") + 1] == "full_access"
        assert args[args.index("--as") + 1] == "bot"
        assert "--yes" in args
        assert run.call_args.kwargs["retries"] == 1

    def test_standard_base_creation_uses_relative_fields_file(self, tmp_path):
        import bitable_client
        from pathlib import Path

        captured: dict[str, Any] = {}

        def fake_run(args, **kwargs):
            fields_path = Path(tmp_path) / "base-fields.json"
            captured["fields"] = json.loads(
                fields_path.read_text(encoding="utf-8")
            )
            return {"ok": True}

        with mock.patch.object(
            bitable_client, "_run_lark", side_effect=fake_run
        ) as run, mock.patch.object(
            bitable_client,
            "lark_cli_work_dir",
            return_value=Path(tmp_path),
        ):
            bitable_client.create_standard_base(
                "公众号文章", "文章列表", identity="bot"
            )
        args = run.call_args.args[0]
        assert args[:2] == ["base", "+base-create"]
        assert args[args.index("--fields") + 1] == "@base-fields.json"
        fields = captured["fields"]
        assert len(fields) == 11
        assert fields[0]["name"] == "文章标题"
        assert any(field["name"] == "文章链接" for field in fields)
        assert "@-" not in args
        assert not (Path(tmp_path) / "base-fields.json").exists()
        assert run.call_args.kwargs["retries"] == 1

    def test_created_base_identifiers_are_extracted_without_guessing(self):
        import bitable_client

        payload = {
            "ok": True,
            "data": {
                "base": {"app_token": "bascn_created"},
                "created_table_id": "tbl_created",
            },
        }
        assert bitable_client.created_base_identifiers(payload) == (
            "bascn_created",
            "tbl_created",
        )

    def test_permission_error_is_not_retried(self, monkeypatch: pytest.MonkeyPatch):
        import bitable_client

        result = mock.Mock(
            returncode=1,
            stdout="",
            stderr=json.dumps(
                {"ok": False, "error": {"code": 91403, "message": "no permission"}}
            ),
        )
        run = mock.Mock(return_value=result)
        monkeypatch.setattr(bitable_client, "_lark_cli", lambda: "lark-cli")
        monkeypatch.setattr(bitable_client.subprocess, "run", run)
        with pytest.raises(bitable_client.LarkCLIError) as error:
            bitable_client._run_lark(["base", "+field-list"], retries=3)
        assert error.value.kind == "permission"
        assert run.call_count == 1

    def test_transient_conflict_is_retried(self, monkeypatch: pytest.MonkeyPatch):
        import bitable_client

        conflict = mock.Mock(
            returncode=1,
            stdout="",
            stderr=json.dumps(
                {"ok": False, "error": {"code": 1254291, "message": "try again"}}
            ),
        )
        success = mock.Mock(returncode=0, stdout='{"ok":true,"data":{}}', stderr="")
        run = mock.Mock(side_effect=[conflict, success])
        monkeypatch.setattr(bitable_client, "_lark_cli", lambda: "lark-cli")
        monkeypatch.setattr(bitable_client.subprocess, "run", run)
        monkeypatch.setattr(bitable_client.time, "sleep", lambda _: None)
        assert bitable_client._run_lark(["base", "+field-list"])["ok"] is True
        assert run.call_count == 2

    def test_cli_calls_use_isolated_config_and_work_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import bitable_client
        from paths import data_dir

        result = mock.Mock(returncode=0, stdout='{"ok":true,"data":{}}', stderr="")
        run = mock.Mock(return_value=result)
        monkeypatch.setattr(bitable_client, "_lark_cli", lambda: "lark-cli")
        monkeypatch.setattr(bitable_client.subprocess, "run", run)
        bitable_client._run_lark(["config", "show"], retries=1)
        kwargs = run.call_args.kwargs
        assert kwargs["env"]["LARKSUITE_CLI_CONFIG_DIR"] == str(
            (data_dir() / "lark-cli-home" / ".lark-cli").resolve()
        )
        assert kwargs["cwd"] == (data_dir() / "lark-cli-work").resolve()

    def test_cli_error_redacts_identifiers(self):
        from bitable_client import _redact_cli_error

        args = [
            "--base-token",
            "secret-base",
            "--table-id",
            "secret-table",
            "--token",
            "secret-resource",
            "--member-id",
            "ou_secret",
        ]
        value = _redact_cli_error(
            "secret-base secret-table secret-resource ou_secret failed", args
        )
        assert "secret-base" not in value
        assert "secret-table" not in value
        assert "secret-resource" not in value
        assert "ou_secret" not in value


class TestProcess:
    def valid_config(self, *, feishu=False):
        from config_store import DEFAULT_CONFIG, save_config

        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["wechat"] = {"cookie": "cookie", "token": "token"}
        config["subscriptions"] = [{"name": "Example"}]
        if feishu:
            config["feishu"] = {"base_token": "base", "table_id": "table"}
        save_config(config)
        return config

    def dims(self):
        return json.dumps(
            {
                "技术深度": 8,
                "信息新颖度": 7,
                "分析深度与独立观点": 9,
                "实用参考价值": 6,
                "内容质量与可信度": 8,
            },
            ensure_ascii=False,
        )

    def test_done_does_not_shift_to_next_article(self):
        from process_pending import main
        from queue_helpers import add_pending, read_queue

        self.valid_config()
        add_pending([article("a"), article("b")])
        assert main(["done", "1", "--dims", self.dims()]) == 0
        queue = read_queue()
        assert [item["title"] for item in queue["pending"]] == ["Article b"]
        assert next(iter(queue["processed"].values()))["article"]["title"] == "Article a"

    def test_local_completion_does_not_require_config(self):
        from process_pending import main
        from queue_helpers import add_pending, read_queue

        add_pending([article("a")])
        assert main(["done", "--link", article("a")["link"], "--dims", self.dims()]) == 0
        processed = next(iter(read_queue()["processed"].values()))
        assert processed["sync_status"] == "not_requested"

    def test_cmd_done_after_dismiss_returns_clean_error(self, capsys):
        import process_pending
        from queue_helpers import add_pending, dismiss_article

        self.valid_config(feishu=True)
        item = article("a")
        add_pending([item])
        dismiss_article(item["link"])
        with mock.patch.object(process_pending, "_sync_entry") as sync:
            result = process_pending.main(
                [
                    "--format",
                    "json",
                    "done",
                    "--link",
                    item["link"],
                    "--dims",
                    self.dims(),
                    "--feishu",
                ]
            )
        assert result == 1
        sync.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ARTICLE_NOT_FOUND"

    def test_cmd_done_race_after_dismiss_does_not_sync(self, capsys):
        import process_pending
        from queue_helpers import add_pending, dismiss_article

        self.valid_config(feishu=True)
        item = article("a")
        add_pending([item])
        dismiss_article(item["link"])
        with mock.patch.object(
            process_pending, "_resolve", return_value=dict(item)
        ) as resolve, mock.patch.object(process_pending, "_sync_entry") as sync:
            result = process_pending.main(
                [
                    "--format",
                    "json",
                    "done",
                    "--link",
                    item["link"],
                    "--dims",
                    self.dims(),
                    "--feishu",
                ]
            )
        assert result == 1
        resolve.assert_called_once()
        sync.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "dismissed" in payload["error"]["message"]

    def test_failed_sync_stays_pending(self):
        import process_pending
        from queue_helpers import add_pending, pending_sync_entries

        self.valid_config(feishu=True)
        add_pending([article("a")])
        with mock.patch.object(
            process_pending, "upsert_article", side_effect=process_pending.LarkCLIError("nope")
        ):
            result = process_pending.main(
                ["done", "--link", article("a")["link"], "--dims", self.dims(), "--feishu"]
            )
        assert result == 1
        assert pending_sync_entries()[0]["article"]["title"] == "Article a"

    def test_successful_sync_marks_exact_article(self):
        import process_pending
        from queue_helpers import add_pending, read_queue

        self.valid_config(feishu=True)
        add_pending([article("a"), article("b")])
        with mock.patch.object(process_pending, "upsert_article") as upsert:
            result = process_pending.main(
                ["done", "--link", article("a")["link"], "--dims", self.dims(), "--feishu"]
            )
        assert result == 0
        assert upsert.call_args.args[1]["title"] == "Article a"
        processed = next(iter(read_queue()["processed"].values()))
        assert processed["sync_status"] == "synced"

    def test_explicit_direct_write_can_override_score_threshold(self):
        import process_pending
        from queue_helpers import add_pending

        self.valid_config(feishu=True)
        add_pending([article("a")])
        low_scores = json.dumps(
            {
                "技术深度": 1,
                "信息新颖度": 1,
                "分析深度与独立观点": 1,
                "实用参考价值": 1,
                "内容质量与可信度": 1,
            },
            ensure_ascii=False,
        )
        with mock.patch.object(process_pending, "upsert_article") as upsert:
            result = process_pending.main(
                [
                    "done",
                    "--link",
                    article("a")["link"],
                    "--dims",
                    low_scores,
                    "--feishu",
                    "--force-feishu",
                ]
            )
        assert result == 0
        upsert.assert_called_once()

    def test_done_dry_run_does_not_mutate_queue(self):
        import process_pending
        from queue_helpers import add_pending, read_queue

        self.valid_config(feishu=True)
        add_pending([article("a")])
        with mock.patch.object(process_pending, "upsert_article") as upsert:
            result = process_pending.main(
                [
                    "done",
                    "--link",
                    article("a")["link"],
                    "--dims",
                    self.dims(),
                    "--feishu",
                    "--dry-run",
                ]
            )
        assert result == 0
        assert upsert.call_args.kwargs["dry_run"] is True
        queue = read_queue()
        assert len(queue["pending"]) == 1
        assert queue["processed"] == {}

    def test_ad_dry_run_does_not_mutate_queue(self):
        from process_pending import main
        from queue_helpers import add_pending, read_queue

        add_pending([article("a")])
        result = main(
            [
                "done",
                "--link",
                article("a")["link"],
                "--ad",
                "--feishu",
                "--dry-run",
            ]
        )
        assert result == 0
        queue = read_queue()
        assert len(queue["pending"]) == 1
        assert queue["processed"] == {}

    def test_dimensions_file_avoids_shell_json_quoting(self, tmp_path: Path):
        from process_pending import main
        from queue_helpers import add_pending, read_queue

        self.valid_config()
        add_pending([article("a")])
        dimensions = tmp_path / "scores.json"
        dimensions.write_text(self.dims(), encoding="utf-8")
        result = main(
            [
                "done",
                "--link",
                article("a")["link"],
                "--dims-file",
                str(dimensions),
            ]
        )
        assert result == 0
        assert not read_queue()["pending"]

    def test_dimensions_file_accepts_utf8_bom(self, tmp_path: Path):
        from process_pending import main
        from queue_helpers import add_pending

        self.valid_config()
        add_pending([article("a")])
        dimensions = tmp_path / "scores-bom.json"
        dimensions.write_text(self.dims(), encoding="utf-8-sig")
        assert main(
            ["done", "--link", article("a")["link"], "--dims-file", str(dimensions)]
        ) == 0

    def test_inbox_filters_pending_and_processed_articles(self, capsys):
        import process_pending
        from queue_helpers import add_pending, complete_article

        first = article("a")
        first.update({"title": "AI systems", "account": "Research", "update_time": 100})
        second = article("b")
        second.update({"title": "Product notes", "account": "Product", "update_time": 200})
        add_pending([first, second])
        complete_article(
            second["link"],
            {"score": 8.2, "summary": "AI product summary", "tags": ["AI"]},
        )
        assert process_pending.main(
            [
                "--format",
                "json",
                "inbox",
                "--status",
                "all",
                "--query",
                "AI",
                "--sort",
                "newest",
            ]
        ) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["summary"] == {
            "pending": 1,
            "processed": 1,
            "favorites": 0,
            "later": 0,
            "dismissed": 0,
            "sync_pending": 0,
            "matched": 2,
            "returned": 2,
        }
        assert [item["article"]["title"] for item in payload["data"]["items"]] == [
            "Product notes",
            "AI systems",
        ]

    def test_inbox_limit_is_validated(self, capsys):
        import process_pending

        assert process_pending.main(
            ["--format", "json", "inbox", "--limit", "0"]
        ) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False

    def test_inbox_commands_manage_favorite_later_dismiss_and_restore(self, capsys):
        import process_pending
        from queue_helpers import add_pending

        item = article("a")
        add_pending([item])
        assert process_pending.main(
            [
                "--format",
                "json",
                "inbox-mark",
                "--link",
                item["link"],
                "--favorite",
                "--later",
            ]
        ) == 0
        marked = json.loads(capsys.readouterr().out)
        assert marked["data"]["favorite"] is True
        assert marked["data"]["inbox_state"] == "later"
        assert process_pending.main(
            ["--format", "json", "inbox", "--favorite", "--state", "later"]
        ) == 0
        inbox = json.loads(capsys.readouterr().out)
        assert inbox["data"]["summary"]["matched"] == 1
        assert process_pending.main(
            ["--format", "json", "dismiss", "--link", item["link"]]
        ) == 0
        dismissed = json.loads(capsys.readouterr().out)
        assert dismissed["data"]["reversible"] is True
        assert process_pending.main(
            [
                "--format",
                "json",
                "inbox",
                "--status",
                "processed",
                "--disposition",
                "dismissed",
            ]
        ) == 0
        processed = json.loads(capsys.readouterr().out)
        assert processed["data"]["summary"]["matched"] == 1
        assert process_pending.main(
            ["--format", "json", "restore", "--link", item["link"]]
        ) == 0
        restored = json.loads(capsys.readouterr().out)
        assert restored["data"]["status"] == "pending"

    def test_digest_plan_applies_preferences_without_fetching_or_completing(self, capsys):
        import time

        import process_pending
        from config_store import DEFAULT_CONFIG, save_config
        from queue_helpers import add_pending, read_queue, update_inbox_item

        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["preferences"].update(
            {
                "include_topics": ["AI"],
                "exclude_keywords": ["招聘"],
                "preferred_accounts": ["Research"],
                "digest_hours": 24,
                "digest_limit": 2,
            }
        )
        save_config(config)
        now = int(time.time())
        preferred = {
            **article("a"),
            "title": "AI systems",
            "account": "Research",
            "update_time": now,
        }
        favorite = {
            **article("b"),
            "title": "Product review",
            "account": "Product",
            "update_time": now - 10,
        }
        blocked = {
            **article("c"),
            "title": "AI 招聘",
            "account": "Research",
            "update_time": now - 20,
        }
        later = {
            **article("d"),
            "title": "AI later",
            "account": "Research",
            "update_time": now - 30,
        }
        add_pending([preferred, favorite, blocked, later])
        update_inbox_item(favorite["link"], favorite=True)
        update_inbox_item(later["link"], state="later")
        assert process_pending.main(["--format", "json", "digest-plan"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [item["title"] for item in payload["data"]["candidates"]] == [
            "Product review",
            "AI systems",
        ]
        assert payload["data"]["candidates"][0]["link"] == favorite["link"]
        assert payload["data"]["candidates"][0]["url"] == favorite["link"]
        assert payload["data"]["excluded"] == {
            "too_old": 0,
            "later": 1,
            "keyword": 1,
        }
        assert payload["data"]["content_fetched"] is False
        assert payload["data"]["articles_completed"] is False
        assert payload["next_action"] == "read_score_digest_candidates"
        assert len(read_queue()["pending"]) == 4


def test_runtime_can_use_ready_system_python(monkeypatch: pytest.MonkeyPatch):
    import runtime

    completed = mock.Mock(returncode=0)
    monkeypatch.setattr(runtime, "_venv_python", lambda: Path("missing-python"))
    monkeypatch.setattr(runtime.subprocess, "run", mock.Mock(return_value=completed))
    assert runtime.main(["process", "--help"]) == 0
    assert runtime.subprocess.run.call_args.args[0][0] == str(Path(runtime.sys.executable))


def test_queue_only_process_command_does_not_require_article_parser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import builtins
    import importlib

    original_import = builtins.__import__
    sys.modules.pop("process_pending", None)
    sys.modules.pop("article_reader", None)

    def block_article_reader(name, *args, **kwargs):
        if name == "article_reader":
            raise ModuleNotFoundError("article parser is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_article_reader)
    process_pending = importlib.import_module("process_pending")

    assert process_pending.main(["list"]) == 0
    assert "No pending articles" in capsys.readouterr().out


def test_runtime_allows_local_process_commands_without_article_dependencies():
    import runtime

    assert runtime._system_runtime_is_ready("process") is True


def test_runtime_venv_follows_state_override():
    import runtime

    expected_root = Path(os.environ["WECHAT_ARTICLE_HOME"]).resolve() / "venv"
    expected = expected_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert runtime._venv_python() == expected
