"""Narrow adapter for a configured Feishu target and its permitted operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bitable_client import LarkCLIError


class FeishuTarget:
    """Bind a target configuration to the small set of operations callers need."""

    def __init__(
        self,
        feishu: dict[str, Any],
        *,
        cli_info: Callable[[], dict[str, Any]],
        preflight: Callable[[dict[str, Any]], dict[str, Any]],
        upsert: Callable[[dict[str, Any], dict[str, Any], dict[str, Any], bool], None],
    ) -> None:
        self._feishu = feishu
        self._cli_info = cli_info
        self._preflight = preflight
        self._upsert = upsert

    def check(self) -> dict[str, Any]:
        """Verify CLI compatibility, identity, permissions, and field mapping."""
        if not self._feishu.get("enabled"):
            raise LarkCLIError(
                "Feishu sync is disabled; complete Agent setup first", kind="config"
            )
        cli = self._cli_info()
        if not cli.get("compatible"):
            raise LarkCLIError(
                f"lark-cli {cli.get('version', 'unknown')} is outside the supported range >=1.0.69,<2",
                kind="version",
            )
        return self._preflight(self._feishu)

    def sync(
        self, article: dict[str, Any], metadata: dict[str, Any], *, dry_run: bool = False
    ) -> None:
        """Upsert one processed article to this already-configured target."""
        if not self._feishu.get("enabled"):
            raise LarkCLIError(
                "Feishu sync is disabled; complete Agent setup first", kind="config"
            )
        self._upsert(self._feishu, article, metadata, dry_run)
