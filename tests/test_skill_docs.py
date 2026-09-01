"""Guards SKILL.md/reference invariants that code-level tests cannot see.

The provisioning guidance lives in three places (manage.py canned commands,
SKILL.md, references/); these checks keep the documentation side from drifting
back into patterns the code fixes already removed.
"""

from __future__ import annotations

import re
from pathlib import Path


SKILL_DIR = (
    Path(__file__).resolve().parents[1] / "skills" / "wechat-article-subscriber"
)
DOC_FILES = [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]


def _doc_texts() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in DOC_FILES
        if path.exists()
    }


def test_create_base_examples_never_append_bare_yes():
    # Matches only the dangerous command form "--table-name <X> --yes";
    # prose such as "without `--yes`" or "--table-name <X>` without --yes"
    # does not match because the flag is not a direct argument suffix.
    forbidden = re.compile(r"feishu-create-base[^\n]*--table-name\s+\S+\s+--yes\b")
    for name, text in _doc_texts().items():
        match = forbidden.search(text)
        assert match is None, (
            f"{name}: create-base example must route authorization through the "
            f"persisted policy, not a bare --yes: {match.group(0)!r}"
        )


def test_workflow_step_numbers_are_sequential():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    numbers = [int(value) for value in re.findall(r"^(\d+)\. ", text, re.MULTILINE)]
    assert numbers, "workflow steps not found in SKILL.md"
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"duplicate or gapped workflow step numbering: {numbers}"
    )


def test_indented_shell_lines_use_list_continuation_indent():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if stripped.startswith("bash scripts/run.sh") and line != stripped:
            indent = len(line) - len(stripped)
            assert indent == 3, (
                f"indented command lines must use the 3-space list continuation "
                f"indent (found {indent}): {line!r}"
            )


def test_description_is_multiline_with_paid_boundary_and_casual_triggers():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md frontmatter is missing"
    description_lines: list[str] = []
    collecting = False
    for line in match.group(1).splitlines():
        if line.startswith("description:"):
            assert line.rstrip() == "description: |", (
                "description must use the YAML multiline | form so future colons "
                "cannot break frontmatter parsing"
            )
            collecting = True
            continue
        if collecting and line.startswith("  "):
            description_lines.append(line[2:])
        elif collecting:
            break
    description = " ".join(part.strip() for part in description_lines).strip()
    assert description, "description body is empty"
    assert 200 <= len(description) <= 600, (
        f"description length {len(description)} outside the 200-600 char window"
    )
    # Boundary: the paid per-call nature must surface at trigger time.
    assert "paid" in description and "redfox.hk" in description
    # Casual trigger phrases that map to real capabilities
    # (digest-plan, discover, process export, subscriptions remove).
    for trigger in ("日报", "追更", "导出", "退订"):
        assert trigger in description, f"missing casual trigger phrase: {trigger}"
