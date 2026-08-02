"""Feishu target seam tests with in-memory adapters."""

from __future__ import annotations

import pytest


def make_target(feishu=None, cli=None, preflight=None, upsert=None):
    from feishu_target import FeishuTarget

    return FeishuTarget(
        feishu if feishu is not None else {"enabled": True},
        cli_info=cli or (lambda: {"compatible": True, "version": "1.0.69"}),
        preflight=preflight or (lambda feishu: {"identity": "user", "mapping": {}}),
        upsert=upsert or (lambda feishu, article, metadata, dry_run=False: None),
    )


def test_check_disabled_raises_config_kind():
    from bitable_client import LarkCLIError

    target = make_target(feishu={"enabled": False})
    with pytest.raises(LarkCLIError) as excinfo:
        target.check()
    assert excinfo.value.kind == "config"


def test_check_incompatible_cli_raises_version_kind():
    from bitable_client import LarkCLIError

    target = make_target(cli=lambda: {"compatible": False, "version": "0.9"})
    with pytest.raises(LarkCLIError) as excinfo:
        target.check()
    assert excinfo.value.kind == "version"


def test_check_passes_preflight_result_through():
    target = make_target(
        preflight=lambda feishu: {"identity": "user", "mapping": {"title": "x"}}
    )
    assert target.check() == {"identity": "user", "mapping": {"title": "x"}}


def test_sync_disabled_raises_config_kind():
    from bitable_client import LarkCLIError

    target = make_target(feishu={"enabled": False})
    with pytest.raises(LarkCLIError) as excinfo:
        target.sync({"title": "a"}, {})
    assert excinfo.value.kind == "config"


def test_sync_passes_through_with_dry_run():
    calls: list[tuple] = []

    def upsert(feishu, article, metadata, dry_run=False):
        calls.append((article, metadata, dry_run))

    target = make_target(upsert=upsert)
    target.sync({"title": "a"}, {"score": 8.0}, dry_run=True)
    assert calls == [({"title": "a"}, {"score": 8.0}, True)]


def test_production_factory_wires_real_adapters_without_lark_cli():
    from bitable_client import LarkCLIError
    from feishu_target import production_feishu_target

    target = production_feishu_target({"enabled": False})
    with pytest.raises(LarkCLIError) as excinfo:
        target.check()
    assert excinfo.value.kind == "config"
