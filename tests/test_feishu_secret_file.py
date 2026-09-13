"""File-based Feishu App Secret intake: prepare → edit → consume."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "state"
    monkeypatch.setenv("WECHAT_ARTICLE_HOME", str(home))
    return home


def _secret_ready_config(home: Path) -> None:
    from config_store import save_config, validate_config

    config = validate_config(
        {
            "version": 11,
            "redfox": {"api_key": "k"},
            "subscriptions": [{"name": "人民日报", "alias": "rmrb"}],
            "setup": {"feishu_identity_confirmed": True},
            "feishu": {
                "destination": "create",
                "identity": "user",
                "expected_app_id": "cli_confirmed123",
                "cli_profile": "wechat-article-profile",
            },
        }
    )
    save_config(config)


def _secret_file(home: Path) -> Path:
    from manage_feishu import _secret_file_path

    return _secret_file_path()


def _prepare(home: Path) -> dict:
    import manage_feishu

    _secret_ready_config(home)
    result, action = manage_feishu.feishu_app_secret(
        SimpleNamespace(
            app_id="",
            prepare_secret_file=True,
            open_secret_file=False,
            secret_file="",
        )
    )
    assert action == "edit_then_consume_feishu_secret_file"
    return result


def test_prepare_creates_restricted_placeholder_file(isolated_home):
    result = _prepare(isolated_home)
    path = Path(result["path"])
    assert path == _secret_file(isolated_home)
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip() == "PASTE_APP_SECRET_HERE"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert result["consume_command"].endswith(path.name)
    assert "instructions" in result
    assert result["contents_echoed"] is False


def test_consume_strips_whitespace_stores_and_deletes(isolated_home, monkeypatch):
    import manage_feishu

    _prepare(isolated_home)
    path = _secret_file(isolated_home)
    path.write_text("  SECRET123  \n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run_lark(argv, **kwargs):
        captured["argv"] = argv
        captured["input_text"] = kwargs.get("input_text")

    monkeypatch.setattr(manage_feishu, "run_lark", fake_run_lark)
    monkeypatch.setattr(
        manage_feishu,
        "probe_app_secret_resolution",
        lambda: {"resolvable": True},
    )
    result, action = manage_feishu.feishu_app_secret(
        SimpleNamespace(
            app_id="",
            prepare_secret_file=False,
            open_secret_file=False,
            secret_file=str(path),
        )
    )
    assert captured["input_text"] == "SECRET123"
    assert captured["argv"][:2] == ["config", "init"]
    assert result["secret_accepted"] is True
    assert action == "run_feishu_context_then_authorize_only_if_needed"
    assert not path.exists()  # one-time file is consumed


def test_consume_rejects_placeholder_and_keeps_file(isolated_home):
    import manage_feishu

    _prepare(isolated_home)  # leaves the placeholder in place
    path = _secret_file(isolated_home)
    with pytest.raises(Exception, match="placeholder"):
        manage_feishu.feishu_app_secret(
            SimpleNamespace(
                app_id="",
                prepare_secret_file=False,
                open_secret_file=False,
                secret_file=str(path),
            )
        )
    assert path.exists()  # kept so the user can still paste the real secret


def test_consume_rejects_multiline_content(isolated_home):
    import manage_feishu

    _prepare(isolated_home)
    path = _secret_file(isolated_home)
    path.write_text("SECRET123\nsecond-line\n", encoding="utf-8")
    with pytest.raises(Exception, match="single line"):
        manage_feishu.feishu_app_secret(
            SimpleNamespace(
                app_id="",
                prepare_secret_file=False,
                open_secret_file=False,
                secret_file=str(path),
            )
        )
    assert path.exists()


def test_consume_rejects_paths_outside_prepared_location(isolated_home, tmp_path):
    import manage_feishu

    _secret_ready_config(isolated_home)
    foreign = tmp_path / "not-the-inbox.txt"
    foreign.write_text("SECRET123\n", encoding="utf-8")
    with pytest.raises(Exception, match="prepared file"):
        manage_feishu.feishu_app_secret(
            SimpleNamespace(
                app_id="",
                prepare_secret_file=False,
                open_secret_file=False,
                secret_file=str(foreign),
            )
        )
    assert foreign.exists()


def test_open_requires_prepared_file(isolated_home):
    import manage_feishu

    _secret_ready_config(isolated_home)
    with pytest.raises(Exception, match="prepare it first"):
        manage_feishu.feishu_app_secret(
            SimpleNamespace(
                app_id="",
                prepare_secret_file=False,
                open_secret_file=True,
                secret_file="",
            )
        )


def test_wizard_emits_file_flow_not_shell_pipe(isolated_home, monkeypatch):
    import manage_feishu
    from config_store import load_config, save_config

    _secret_ready_config(isolated_home)
    config = load_config()
    config["feishu"]["identity"] = "bot"
    save_config(config)
    monkeypatch.setattr(
        manage_feishu,
        "private_profile_secret_state",
        lambda: {"bound": True, "profile": "wechat-article-profile", "ready": False},
    )
    state, action = manage_feishu.feishu_setup()
    assert action == "provide_app_secret_for_private_profile"
    assert "--prepare-secret-file" in state["next_command"]
    assert "--secret-file" in state["next_command"]
    assert "printf" not in state["next_command"]


def test_all_data_reset_removes_unconsumed_secret_file(isolated_home):
    import manage
    from config_store import config_path

    _secret_ready_config(isolated_home)
    path = _secret_file(isolated_home)
    path.write_text("SECRET123\n", encoding="utf-8")
    manage._reset(SimpleNamespace(scope="all-data", yes=True))
    assert not path.exists()
    assert not config_path().exists()
