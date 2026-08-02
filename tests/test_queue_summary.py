"""Queue summary and known-URL query tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))


def article(letter: str) -> dict:
    return {
        "title": f"Article {letter}",
        "link": f"https://mp.weixin.qq.com/s/{letter}",
        "digest": f"Digest {letter}",
        "account": "Example",
        "update_time": 1_700_000_000,
    }


def test_queue_summary_counts_mixed_state():
    from article_inbox import queue_summary
    from queue_helpers import (
        add_pending,
        complete_article,
        dismiss_article,
        update_inbox_item,
    )

    add_pending([article("a"), article("b"), article("c"), article("d")])
    update_inbox_item("https://mp.weixin.qq.com/s/a", favorite=True)
    update_inbox_item("https://mp.weixin.qq.com/s/b", state="later")
    complete_article(
        "https://mp.weixin.qq.com/s/c",
        {"disposition": "completed", "score": 8.0},
        sync_status="pending",
    )
    dismiss_article("https://mp.weixin.qq.com/s/d")

    summary = queue_summary()
    assert summary["pending"] == 2
    assert summary["processed"] == 2
    assert summary["favorites"] == 1
    assert summary["later"] == 1
    assert summary["dismissed"] == 1
    assert summary["sync_pending"] == 1


def test_queue_summary_empty():
    from article_inbox import queue_summary

    assert queue_summary() == {
        "pending": 0,
        "processed": 0,
        "favorites": 0,
        "later": 0,
        "dismissed": 0,
        "sync_pending": 0,
    }


def test_known_urls_returns_pending_and_processed_identities():
    from article_inbox import known_urls
    from queue_helpers import add_pending, complete_article

    add_pending([article("a"), article("b")])
    complete_article("https://mp.weixin.qq.com/s/a", {"disposition": "completed"})

    assert known_urls() == {
        "https://mp.weixin.qq.com/s/a",
        "https://mp.weixin.qq.com/s/b",
    }


def test_doctor_queue_block_uses_same_summary():
    import manage
    from article_inbox import queue_summary
    from config_store import DEFAULT_CONFIG, save_config
    from queue_helpers import add_pending, complete_article, update_inbox_item

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["wechat"] = {"cookie": "c", "token": "t"}
    config["subscriptions"] = [{"name": "Example"}]
    save_config(config)

    add_pending([article("a"), article("b"), article("c")])
    update_inbox_item("https://mp.weixin.qq.com/s/a", favorite=True)
    update_inbox_item("https://mp.weixin.qq.com/s/b", state="later")
    complete_article("https://mp.weixin.qq.com/s/c", {"disposition": "completed"})

    report, _ = manage._doctor(online=False, save_resolved=False)
    summary = queue_summary()
    assert report["queue"] == {
        "total": summary["pending"] + summary["processed"],
        **summary,
    }
