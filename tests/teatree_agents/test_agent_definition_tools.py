"""Every ``agents/*.md`` definition must expose the ``Skill`` tool.

A sub-agent spawned through the harness ``Agent`` tool receives exactly the tools
its definition's ``tools:`` allowlist names. Omitting ``Skill`` closes BOTH skill
channels at once: the agent cannot load a skill itself, and the ``skills:``
frontmatter it declares is honoured only by teatree's OWN dispatch path
(``build_system_context`` / ``_read_skill_contents``), which a raw ``Agent``-tool
spawn bypasses. The observed symptom was sub-agents reporting that the ``Skill``
tool was not in their toolset and then running without their mandated skills.

``Skill`` only loads instructions into context — it grants no write capability —
so a read-only agent (reviewer, planner, triage-assessor) carries it safely.

The frontmatter is parsed from the real repo ``agents/*.md`` files on disk, never
a fixture copy, so a future definition added without ``Skill`` fails here.
"""

from pathlib import Path
from typing import Any

import yaml
from django.test import TestCase

#: Repo-root ``agents/`` directory — the canonical sub-agent definitions the
#: ``Agent`` tool resolves a ``t3:<name>`` value against.
AGENTS_DIR: Path = Path(__file__).resolve().parents[2] / "agents"

SKILL_TOOL = "Skill"


def _frontmatter(path: Path) -> dict[str, Any]:
    """Parse the leading ``---``-delimited YAML frontmatter block of *path*."""
    _, _, after_open = path.read_text(encoding="utf-8").partition("---\n")
    block, _, _ = after_open.partition("\n---")
    return yaml.safe_load(block) or {}


class TestAgentDefinitionsExposeTheSkillTool(TestCase):
    def test_agents_dir_holds_definitions(self) -> None:
        # Anti-vacuity floor: an empty glob would make every check below pass.
        assert sorted(AGENTS_DIR.glob("*.md")), f"no agent definitions found under {AGENTS_DIR}"

    def test_every_explicit_tools_allowlist_includes_skill(self) -> None:
        offenders: dict[str, list[str]] = {}
        for path in sorted(AGENTS_DIR.glob("*.md")):
            tools = _frontmatter(path).get("tools")
            if tools is not None and SKILL_TOOL not in tools:
                offenders[path.name] = tools
        assert offenders == {}, (
            f"agent definitions whose 'tools:' allowlist omits {SKILL_TOOL!r}: {offenders}. "
            "A sub-agent spawned via the Agent tool gets exactly this allowlist, so without "
            f"{SKILL_TOOL!r} it can load no skill at all — and its 'skills:' frontmatter is "
            "honoured only by teatree's own dispatch, which a raw Agent spawn bypasses."
        )

    def test_definitions_without_a_tools_key_never_deny_skill(self) -> None:
        # A definition naming no 'tools:' inherits the full toolset and reaches
        # Skill already; adding an allowlist there would NARROW it. Such a
        # definition must not claw Skill back via 'disallowedTools:' either.
        for path in sorted(AGENTS_DIR.glob("*.md")):
            frontmatter = _frontmatter(path)
            if frontmatter.get("tools") is not None:
                continue
            denied = frontmatter.get("disallowedTools") or []
            assert SKILL_TOOL not in denied, (
                f"{path.name} inherits the full toolset but denies {SKILL_TOOL!r} — it could load no skill"
            )
