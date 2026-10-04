"""Single-purpose execution-policy decisions shared by setup and processing commands."""

from __future__ import annotations

from typing import Any

from config_transitions import (
    feishu_approval_scope_changed as _feishu_approval_scope_changed,
    invalidate_for_feishu_change as _invalidate_for_feishu_change,
    invalidate_policy as _invalidate_policy,
)
from feishu_setup import next_stage as _next_stage, stage_facts as _stage_facts

feishu_approval_scope_changed = _feishu_approval_scope_changed
invalidate_for_feishu_change = _invalidate_for_feishu_change
invalidate_policy = _invalidate_policy
next_stage = _next_stage
stage_facts = _stage_facts


def policy_for(config: dict[str, Any]) -> dict[str, Any]:
    """Return the persisted policy object owned by a configuration."""
    return config["setup"]["execution_policy"]


def autopilot_policy(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return a confirmed autopilot policy, or ``None`` when approval is absent."""
    policy = policy_for(config)
    if policy["confirmed"] and policy["mode"] == "autopilot":
        return policy
    return None


def allows_automatic_provisioning(
    config: dict[str, Any], *, base_name: str, table_name: str
) -> bool:
    """Check whether the exact requested Base creation was pre-approved."""
    policy = policy_for(config)
    return bool(
        policy["confirmed"]
        and policy["mode"] == "autopilot"
        and config["feishu"]["destination"] == "create"
        and policy["allow_feishu_provisioning"]
        and policy["provision_base_name"] == base_name
        and policy["provision_table_name"] == table_name
        and not config["feishu"]["base_token"]
        and not config["feishu"]["table_id"]
    )
