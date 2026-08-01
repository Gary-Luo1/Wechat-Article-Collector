"""Validate the portable skill bundle and repository adapter without network access."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "wechat-article-subscriber"
ADAPTERS = (
    ROOT / ".agents" / "skills" / "wechat-article-subscriber",
    ROOT / ".claude" / "skills" / "wechat-article-subscriber",
    ROOT / ".github" / "skills" / "wechat-article-subscriber",
)


def fail(message: str) -> None:
    raise ValueError(message)


def read_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        fail(f"{path.relative_to(ROOT)} must start with YAML frontmatter")
    parts = text.split("---\n", 2)
    if len(parts) != 3:
        fail(f"{path.relative_to(ROOT)} frontmatter is not closed")
    fields = {}
    for line in parts[1].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    if set(fields) != {"name", "description"}:
        fail(f"{path.relative_to(ROOT)} frontmatter must contain only name and description")
    return fields, text


def validate_skill() -> None:
    skill_md = SKILL / "SKILL.md"
    fields, text = read_frontmatter(skill_md)
    if fields["name"] != SKILL.name or not re.fullmatch(r"[a-z0-9-]{1,64}", fields["name"]):
        fail("skill name is invalid or does not match the directory")
    if not fields["description"] or len(fields["description"]) > 1024:
        fail("skill description must contain 1-1024 characters")
    if len(text.splitlines()) > 500:
        fail("SKILL.md exceeds 500 lines")
    for reference in re.findall(r"\((references/[^)]+)\)", text):
        if not (SKILL / reference).is_file():
            fail(f"missing referenced file: {reference}")
    forbidden = {
        "README.md",
        "CHANGELOG.md",
        "config.json",
        "queue.json",
        "test_core.py",
        "pyproject.toml",
    }
    present = forbidden.intersection(path.name for path in SKILL.iterdir())
    if present:
        fail(f"skill bundle contains repository/runtime files: {sorted(present)}")
    allowed = {"SKILL.md", "requirements.txt", "agents", "scripts", "references", "assets"}
    generated = {".pytest_cache", "__pycache__"}
    unexpected = {path.name for path in SKILL.iterdir()} - allowed - generated
    if unexpected:
        fail(f"skill bundle contains unexpected top-level paths: {sorted(unexpected)}")
    for wrapper in (SKILL / "scripts" / "run.sh", SKILL / "scripts" / "run.ps1"):
        if not wrapper.is_file():
            fail(f"missing platform wrapper: {wrapper.relative_to(ROOT)}")
    for required in (
        SKILL / "scripts" / "manage.py",
        SKILL / "scripts" / "protocol.py",
        SKILL / "references" / "feishu.md",
        SKILL / "references" / "operations.md",
        SKILL / "references" / "automation.md",
        ROOT / "tools" / "package_github_source.py",
    ):
        if not required.is_file():
            fail(f"missing operational release file: {required.relative_to(ROOT)}")


def validate_adapters() -> None:
    canonical_reference = "../../../skills/wechat-article-subscriber/SKILL.md"
    for adapter in ADAPTERS:
        skill_md = adapter / "SKILL.md"
        fields, text = read_frontmatter(skill_md)
        if fields["name"] != "wechat-article-subscriber":
            fail(f"adapter name is invalid: {skill_md.relative_to(ROOT)}")
        if canonical_reference not in text:
            fail(f"adapter does not reference canonical Skill: {skill_md.relative_to(ROOT)}")
        contents = {path.name for path in adapter.iterdir()}
        if contents != {"SKILL.md"}:
            fail(f"adapter must not duplicate implementation files: {adapter.relative_to(ROOT)}")


def validate_plugin() -> None:
    manifest_path = ROOT / ".codex-plugin" / "plugin.json"
    raw = manifest_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        fail("plugin.json must not contain a UTF-8 BOM")
    manifest = json.loads(raw.decode("utf-8"))
    allowed = {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "skills",
        "interface",
    }
    unsupported = set(manifest) - allowed
    if unsupported:
        fail(f"plugin.json contains unsupported fields: {sorted(unsupported)}")
    if manifest.get("name") != "wechat-article-subscriber":
        fail("plugin name is invalid")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("version", ""))):
        fail("plugin version must use strict semver")
    skills = manifest.get("skills")
    if not isinstance(skills, str) or not skills.startswith("./"):
        fail("plugin skills must be one relative string beginning with ./")
    if not (ROOT / skills).is_dir():
        fail("plugin skills path does not exist")
    required_interface = {
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "defaultPrompt",
    }
    missing = required_interface - set(manifest.get("interface", {}))
    if missing:
        fail(f"plugin interface is missing: {sorted(missing)}")


def validate_version_consistency(
    plugin_path: Path | None = None,
    changelog_path: Path | None = None,
) -> None:
    """Fail when the plugin manifest version drifts from the changelog head."""
    manifest_path = plugin_path or (ROOT / ".codex-plugin" / "plugin.json")
    history_path = changelog_path or (ROOT / "CHANGELOG.md")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plugin_version = str(manifest.get("version", ""))
    match = re.search(
        r"^##\s+(\d+\.\d+\.\d+)",
        history_path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        fail("CHANGELOG.md must start with a semantic version heading")
    changelog_version = match.group(1)
    if plugin_version != changelog_version:
        fail(
            f"plugin.json version {plugin_version} does not match "
            f"CHANGELOG version {changelog_version}"
        )


def main() -> int:
    try:
        validate_skill()
        validate_adapters()
        validate_plugin()
        validate_version_consistency()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1
    print("release validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
