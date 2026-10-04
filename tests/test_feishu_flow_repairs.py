"""Feishu setup order, remembered targets, expired auth, and qualified sync."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "state"
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(home))
    return home


def _ready_config() -> dict:
    from config_store import DEFAULT_CONFIG

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["redfox"]["api_key"] = "key"
    config["setup"]["search_window_confirmed"] = True
    config["setup"]["feishu_identity_confirmed"] = True
    config["subscriptions"] = [{"name": "Example", "alias": "example"}]
    config["feishu"].update({"destination": "existing", "identity": "user"})
    return config


def test_next_stage_asks_for_app_id_before_authorization():
    from execution_policy import next_stage

    config = _ready_config()
    stage, action = next_stage(
        config, cli={"compatible": True, "profile_secret": {"ready": False}}
    )
    assert stage == "feishu_app_missing"
    assert action == "select_feishu_app"


def test_next_stage_asks_for_user_secret_before_authorization():
    from execution_policy import next_stage

    config = _ready_config()
    config["feishu"].update({"expected_app_id": "cli_example", "cli_profile": "p1"})
    stage, action = next_stage(
        config,
        cli={
            "compatible": True,
            "profile_secret": {"bound": True, "profile": "p1", "ready": False},
        },
    )
    assert stage == "feishu_secret_missing"
    assert action == "provide_app_secret_for_private_profile"


def test_next_stage_reaches_authorization_after_the_secret_is_stored():
    from execution_policy import next_stage

    config = _ready_config()
    config["feishu"].update({"expected_app_id": "cli_example", "cli_profile": "p1"})
    stage, action = next_stage(
        config,
        cli={
            "compatible": True,
            "profile_secret": {"bound": True, "profile": "p1", "ready": True},
        },
    )
    assert stage == "feishu_authorization_required"
    assert action == "run_feishu_auth_start"


def test_expired_waiting_authorization_starts_a_replacement(isolated_home, monkeypatch):
    import manage_feishu
    from config_store import load_config, save_config

    config = _ready_config()
    started = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    config["feishu"].update({"expected_app_id": "cli_example", "cli_profile": "p1"})
    config["setup"]["feishu_authorization"].update(
        {"state": "waiting", "identity": "user", "started_at": started}
    )
    save_config(config)
    monkeypatch.setattr(
        manage_feishu,
        "feishu_identity_context",
        lambda verify=True: {"app_id_unambiguous": True, "user": {"available": False}},
    )
    result, action = manage_feishu.feishu_auth(SimpleNamespace(auth_command="start", yes=False))
    assert action == "start_single_user_base_authorization"
    assert result["new_authorization_started"] is True
    assert load_config()["setup"]["feishu_authorization"]["state"] == "waiting"
    assert load_config()["setup"]["feishu_authorization"]["started_at"] != started


def test_fresh_waiting_authorization_is_resumed(isolated_home, monkeypatch):
    import manage_feishu
    from config_store import save_config

    config = _ready_config()
    started = datetime.now(timezone.utc).isoformat()
    config["feishu"].update({"expected_app_id": "cli_example", "cli_profile": "p1"})
    config["setup"]["feishu_authorization"].update(
        {"state": "waiting", "identity": "user", "started_at": started}
    )
    save_config(config)
    monkeypatch.setattr(
        manage_feishu,
        "feishu_identity_context",
        lambda verify=True: pytest.fail("a fresh link must not start a second flow"),
    )
    result, action = manage_feishu.feishu_auth(SimpleNamespace(auth_command="start", yes=False))
    assert action == "resume_existing_user_base_authorization"
    assert result["new_authorization_started"] is False


def test_target_url_is_remembered_when_field_listing_fails(isolated_home, monkeypatch):
    import manage_feishu
    from bitable_client import LarkCLIError
    from config_store import load_config, save_config

    config = _ready_config()
    config["setup"]["feishu_authorization"]["state"] = "not_started"
    save_config(config)

    def fail_list(*args, **kwargs):
        raise LarkCLIError("not configured", kind="config")

    monkeypatch.setattr(manage_feishu, "list_fields", fail_list)
    with pytest.raises(LarkCLIError, match="table URL was saved"):
        manage_feishu.feishu_target(
            SimpleNamespace(
                url="https://my.feishu.cn/base/LKoCbskcLas5cosbkZRcL9xwnpg?table=tblzn9HG1WieSsxD"
            )
        )
    saved = load_config()
    assert saved["feishu"]["base_token"] == "LKoCbskcLas5cosbkZRcL9xwnpg"
    assert saved["feishu"]["table_id"] == "tblzn9HG1WieSsxD"
    assert saved["feishu"]["enabled"] is False


def test_qualified_sync_uses_one_preflight_and_skips_low_scores(isolated_home, monkeypatch):
    import process_pending
    from config_store import save_config
    from queue_helpers import add_pending, complete_article, read_queue

    config = _ready_config()
    config["feishu"].update(
        {"enabled": True, "base_token": "base", "table_id": "tbl", "destination": "existing"}
    )
    save_config(config)
    links = {
        "high": "https://mp.weixin.qq.com/s/high",
        "mid": "https://mp.weixin.qq.com/s/mid",
        "low": "https://mp.weixin.qq.com/s/low",
        "ad": "https://mp.weixin.qq.com/s/ad",
    }
    add_pending(
        [
            {"title": name, "link": link, "account": "Example", "digest": name, "update_time": 1}
            for name, link in links.items()
        ]
    )
    complete_article(links["high"], {"score": 7.9, "summary": "a"}, sync_status="not_requested")
    complete_article(links["mid"], {"score": 6.1, "summary": "b"}, sync_status="not_requested")
    complete_article(links["low"], {"score": 4.6, "summary": "c"}, sync_status="not_requested")
    complete_article(links["ad"], {"ad": True}, sync_status="skipped_ad")
    seen: list[dict | None] = []

    def fake_upsert(feishu, art, metadata, *, dry_run=False, preflight_result=None):
        seen.append(preflight_result)
        return {"updated": False, "skipped_fields": [], "preflight": {"marker": "shared"}}

    monkeypatch.setattr("feishu_target.upsert_article", fake_upsert)
    assert process_pending.main(["sync-feishu", "--qualified"]) == 0
    assert [item is None for item in seen] == [True, False]
    assert seen[1] == {"marker": "shared"}
    processed = read_queue()["processed"]
    assert processed[process_pending.normalize_url(links["high"])]["sync_status"] == "synced"
    assert processed[process_pending.normalize_url(links["mid"])]["sync_status"] == "synced"
    assert processed[process_pending.normalize_url(links["low"])]["sync_status"] == "not_requested"
    assert processed[process_pending.normalize_url(links["ad"])]["sync_status"] == "skipped_ad"


def test_link_sync_blocks_low_scores_unless_forced(isolated_home, monkeypatch):
    import process_pending
    from config_store import save_config
    from queue_helpers import add_pending, complete_article, read_queue

    config = _ready_config()
    config["feishu"].update(
        {"enabled": True, "base_token": "base", "table_id": "tbl", "destination": "existing"}
    )
    save_config(config)
    low = "https://mp.weixin.qq.com/s/low-one"
    ad = "https://mp.weixin.qq.com/s/ad-one"
    add_pending(
        [
            {"title": "low", "link": low, "account": "Example", "digest": "d", "update_time": 1},
            {"title": "ad", "link": ad, "account": "Example", "digest": "d", "update_time": 2},
        ]
    )
    complete_article(low, {"score": 5.9, "summary": "s"}, sync_status="not_requested")
    complete_article(ad, {"ad": True}, sync_status="skipped_ad")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("below-threshold and ads must not be written")

    monkeypatch.setattr("feishu_target.upsert_article", fail_if_called)
    assert process_pending.main(["sync-feishu", "--link", low]) == 1
    assert process_pending.main(["sync-feishu", "--link", ad, "--force"]) == 1

    monkeypatch.setattr(
        "feishu_target.upsert_article",
        lambda *args, **kwargs: {"preflight": {"ok": True}},
    )
    assert process_pending.main(["sync-feishu", "--link", low, "--force"]) == 0
    assert read_queue()["processed"][process_pending.normalize_url(low)]["sync_status"] == "synced"


def test_sync_all_requires_an_approved_policy(isolated_home):
    import process_pending
    from config_store import save_config
    from queue_helpers import add_pending, complete_article

    save_config(_ready_config())
    link = "https://mp.weixin.qq.com/s/pending-one"
    add_pending(
        [{"title": "pending", "link": link, "account": "Example", "digest": "d", "update_time": 1}]
    )
    complete_article(link, {"score": 8, "summary": "s"}, sync_status="pending")
    assert process_pending.main(["sync-feishu", "--all"]) == 1


def test_secret_inbox_is_consumed_once(isolated_home, monkeypatch):
    import manage_feishu
    from config_store import save_config

    config = _ready_config()
    config["feishu"].update({"expected_app_id": "cli_example", "cli_profile": "p1"})
    save_config(config)
    prepared, action = manage_feishu.feishu_app_secret(
        SimpleNamespace(
            app_id="",
            prepare_secret_file=False,
            prepare_inbox=True,
            open_secret_file=False,
            secret_file="",
            inbox="",
        )
    )
    assert action == "edit_then_consume_feishu_secret_file"
    path = Path(prepared["path"])
    path.write_text("secret-value\n", encoding="utf-8")
    seen: list[str] = []

    def fake_store(app_id, secret):
        seen.append(secret)
        return {"resolvable": True}

    monkeypatch.setattr(manage_feishu, "_store_app_secret", fake_store)
    result, action = manage_feishu.feishu_app_secret(
        SimpleNamespace(
            app_id="",
            prepare_secret_file=False,
            prepare_inbox=False,
            open_secret_file=False,
            secret_file="",
            inbox=str(path),
        )
    )
    assert result["secret_accepted"] is True
    assert seen == ["secret-value"]
    assert not path.exists()
    assert "secret-value" not in json.dumps(result)
