import json
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CODEX_EVENTS = {
    "PostToolUse",
    "PreCompact",
    "PreToolUse",
    "SessionEnd",
    "SessionStart",
    "Stop",
    "SubagentStop",
    "UserPromptSubmit",
}
_CODEX_EVENTS_WITHOUT_SHARED_POLICY = {"Interrupt", "PermissionRequest", "PostCompact", "SubagentStart"}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_codex_wrapper_declares_shared_package_components() -> None:
    codex = _json(_REPO_ROOT / ".codex-plugin" / "plugin.json")

    assert codex["name"] == "t3"
    assert codex["skills"] == "./skills/"
    assert codex["mcpServers"] == "./.mcp.json"
    assert "hooks" not in codex
    assert "Hooks" not in codex["interface"]["capabilities"]


def test_claude_and_codex_wrappers_share_identity() -> None:
    codex = _json(_REPO_ROOT / ".codex-plugin" / "plugin.json")
    claude = _json(_REPO_ROOT / ".claude-plugin" / "plugin.json")

    assert claude == {
        key: codex[key]
        for key in ("name", "version", "description", "author", "homepage", "repository", "license", "keywords")
    }


def test_both_runtime_wrappers_share_one_mcp_definition() -> None:
    mcp = _json(_REPO_ROOT / ".mcp.json")

    assert mcp["mcpServers"] == {"teatree": {"command": "t3", "args": ["mcp", "serve"]}}


def test_experimental_codex_hook_plan_contains_only_supported_events() -> None:
    hooks = _json(_REPO_ROOT / "hooks" / "codex.json")["hooks"]

    assert set(hooks) == _CODEX_EVENTS
    commands = [hook["command"] for groups in hooks.values() for group in groups for hook in group["hooks"]]
    assert all(
        "codex_hook_adapter.py" in command or "bootstrap-cli.sh" in command or "worker_supervisor.py" in command
        for command in commands
    )
    assert "TaskCreated" not in hooks
    assert "InstructionsLoaded" not in hooks


def test_experimental_codex_hook_plan_omits_events_without_shared_policy() -> None:
    hooks = _json(_REPO_ROOT / "hooks" / "codex.json")["hooks"]

    assert _CODEX_EVENTS_WITHOUT_SHARED_POLICY.isdisjoint(hooks)


def test_experimental_codex_tool_hook_plan_covers_each_tool() -> None:
    hooks = _json(_REPO_ROOT / "hooks" / "codex.json")["hooks"]

    assert [group["matcher"] for group in hooks["PreToolUse"]] == ["*"]
    assert [group["matcher"] for group in hooks["PostToolUse"]] == ["*"]


def test_experimental_codex_session_start_plan_includes_shared_lifecycle() -> None:
    hooks = _json(_REPO_ROOT / "hooks" / "codex.json")["hooks"]

    commands = [hook["command"] for hook in hooks["SessionStart"][0]["hooks"]]
    assert any("bootstrap-cli.sh" in command for command in commands)
    assert any("codex_hook_adapter.py --event SessionStart" in command for command in commands)
    assert any("worker_supervisor.py --event SessionStart" in command for command in commands)


def test_repo_marketplace_points_at_the_same_portable_package() -> None:
    marketplace = _json(_REPO_ROOT / ".agents" / "plugins" / "marketplace.json")

    assert marketplace["name"] == "souliane"
    assert marketplace["plugins"] == [
        {
            "name": "t3",
            "source": {"source": "local", "path": "./plugins/t3"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }
    ]


def test_every_packaged_skill_has_valid_named_yaml_frontmatter() -> None:
    skill_paths = sorted((_REPO_ROOT / "skills").rglob("SKILL.md"))

    assert skill_paths
    for path in skill_paths:
        opening, frontmatter, _body = path.read_text(encoding="utf-8").split("---", 2)
        assert opening == ""
        parsed = yaml.safe_load(frontmatter)
        assert isinstance(parsed, dict), path
        assert isinstance(parsed.get("name"), str), path
        assert parsed["name"].strip(), path
