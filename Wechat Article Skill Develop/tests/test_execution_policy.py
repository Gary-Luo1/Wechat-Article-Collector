"""Execution-policy scope invalidation tests for the widened approval scope."""

from __future__ import annotations

import json

from config_store import DEFAULT_CONFIG
from execution_policy import feishu_approval_scope_changed


NEW_SCOPE_FIELDS = (
    "binding_mode",
    "agent_source",
    "expected_user_open_id",
    "cli_profile",
    "field_mapping",
)


def _feishu() -> dict:
    return json.loads(json.dumps(DEFAULT_CONFIG))["feishu"]


def test_new_scope_field_changes_invalidate_policy():
    for field in NEW_SCOPE_FIELDS:
        previous = _feishu()
        current = dict(previous)
        current[field] = "changed"
        assert feishu_approval_scope_changed(previous, current), field


def test_original_scope_fields_still_invalidate_policy():
    for field in (
        "destination",
        "identity",
        "expected_app_id",
        "manager_open_id",
        "base_token",
        "table_id",
        "schema_policy",
    ):
        previous = _feishu()
        current = dict(previous)
        current[field] = "changed"
        assert feishu_approval_scope_changed(previous, current), field


def test_unrelated_field_change_does_not_invalidate_policy():
    previous = _feishu()
    current = dict(previous)
    current["enabled"] = not previous["enabled"]
    assert feishu_approval_scope_changed(previous, current) is False


def test_identical_config_does_not_invalidate_policy():
    previous = _feishu()
    assert feishu_approval_scope_changed(previous, dict(previous)) is False
