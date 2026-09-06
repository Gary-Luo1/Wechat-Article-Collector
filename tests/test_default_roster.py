from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "skills" / "wechat-article-subscriber" / "assets" / "default_subscriptions.json"


def load_roster() -> list[dict[str, str]]:
    entries = json.loads(ROSTER.read_text(encoding="utf-8"))
    assert isinstance(entries, list)
    return entries


def test_bundled_roster_entries_are_alias_complete_and_unique():
    seen: set[str] = set()
    for index, entry in enumerate(load_roster()):
        assert isinstance(entry, dict), f"roster entry {index} must be an object"
        assert set(entry) <= {"name", "alias", "biz"}, f"roster entry {index} has unsupported keys"
        # Aliases ship with the roster so bulk-add never needs a paid account search.
        assert entry.get("alias"), f"roster entry {index} needs an alias"
        for key, value in entry.items():
            assert isinstance(value, str) and value and value.strip() == value, (
                f"roster entry {index}.{key} must be a non-empty trimmed string"
            )
        identities = {value.casefold() for value in entry.values()}
        assert not identities & seen, f"roster entry {index} duplicates an earlier identity"
        seen.update(identities)


def test_bundled_roster_applies_cleanly_through_bulk_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    roster = load_roster()
    if not roster:
        pytest.skip("bundled roster is empty until the author fills it in")
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(tmp_path / "state"))
    from config_store import DEFAULT_CONFIG, load_config, save_config

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["redfox"] = {"api_key": "redfox-key-secret"}
    config["subscriptions"] = []
    save_config(config)

    import manage

    assert manage.main(["subscriptions", "bulk-add", "--file", str(ROSTER)]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["data"]["added_count"] == len(roster)
    assert applied["data"]["skipped_duplicates"] == []
    assert load_config()["subscriptions"] == roster

    # Re-applying the same roster is idempotent: every entry is a duplicate now.
    assert manage.main(["subscriptions", "bulk-add", "--file", str(ROSTER)]) == 0
    reapplied = json.loads(capsys.readouterr().out)
    assert reapplied["data"]["added_count"] == 0
    assert len(reapplied["data"]["skipped_duplicates"]) == len(roster)
    assert load_config()["subscriptions"] == roster
