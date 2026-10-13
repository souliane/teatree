"""Shipped hook and skill pin contracts across their consumer surfaces."""

import json
import re
from pathlib import Path

from teatree.llm.builtin_tools import KNOWN_BUILTIN_TOOLS

ROOT = Path(__file__).resolve().parents[2]


def _matchers(event: str) -> set[str]:
    manifest = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    return {entry["matcher"] for entry in manifest["hooks"][event] if entry.get("matcher") != "*"}


def test_task_list_tools_are_registered_in_post_tool_matcher() -> None:
    matchers = _matchers("PostToolUse")
    assert any({"TaskCreate", "TaskUpdate"} <= set(matcher.split("|")) for matcher in matchers)


def test_codex_matchers_forward_all_tools_to_shared_router() -> None:
    manifest = json.loads((ROOT / "hooks/codex.json").read_text(encoding="utf-8"))
    for event in ("PreToolUse", "PostToolUse"):
        entries = manifest["hooks"][event]
        assert len(entries) == 1
        assert entries[0]["matcher"] == "*"
        assert f"codex_hook_adapter.py --event {event}" in entries[0]["hooks"][0]["command"]


def test_documented_pre_tool_matchers_equal_shipped_matchers() -> None:
    doc = (ROOT / "hooks/CLAUDE.md").read_text(encoding="utf-8")
    claim = re.search(r"registered PreToolUse matchers are (.*?), so the two", doc)
    assert claim is not None
    assert set(re.findall(r"`([^`]+)`", claim.group(1))) == _matchers("PreToolUse")


def test_optional_glab_matcher_is_documented_as_dormant() -> None:
    services = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "glab" not in services
    doc = (ROOT / "hooks/CLAUDE.md").read_text(encoding="utf-8")
    assert "reserved for an optional glab MCP server" in doc


def test_skill_dependency_and_hook_share_revision() -> None:
    dependencies = (ROOT / "apm.yml").read_text(encoding="utf-8")
    pins = set(re.findall(r"souliane/skills/[^#\s]+#([0-9a-f]{40})", dependencies))
    hooks = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook_pin = re.search(r"repo: https://github.com/souliane/skills\s+rev: ([0-9a-f]{40})", hooks)
    assert hook_pin is not None
    assert pins == {hook_pin.group(1)}


def test_configuration_has_one_per_overlay_heading() -> None:
    doc = (ROOT / "docs/blueprint/configuration.md").read_text(encoding="utf-8")
    assert len(re.findall(r"^### 10\.1\.1 ", doc, re.MULTILINE)) == 1
    assert len(re.findall(r"^### 10\.1\.[1-3] ", doc, re.MULTILINE)) == 3


def test_owned_repo_approval_documentation_uses_overlay_config() -> None:
    for name in (
        "BLUEPRINT.md",
        "docs/blueprint/configuration.md",
        "skills/rules/references/publishing-mode-doctrine.md",
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if "require_owned_repo_approval" in line]
        assert lines
        assert any("OverlayConfig" in line for line in lines), name
        assert all("private DB `ConfigSetting` store" not in line for line in lines), name


def test_eval_readme_describes_generated_promotion_artifact() -> None:
    doc = (ROOT / "evals/README.md").read_text(encoding="utf-8")
    assert "evals/scenarios/promoted_drift.yaml" not in doc
    assert "when promotion actually runs" in doc


def test_hooks_only_name_known_literal_tools() -> None:
    known = set(KNOWN_BUILTIN_TOOLS) | {"CronCreate", "CronDelete", "ScheduleWakeup", "Skill"}
    for event in ("PreToolUse", "PostToolUse"):
        for matcher in _matchers(event):
            if matcher.startswith("mcp__"):
                continue
            assert set(matcher.split("|")) <= known, (event, matcher)
