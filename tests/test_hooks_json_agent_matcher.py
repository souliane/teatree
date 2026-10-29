"""The ``Agent`` PreToolUse matcher is wired in hooks.json (#1646).

The orchestrator-boundary Agent arm and the dispatch-quote Agent arm both ride
``PreToolUse``, but a gate only fires for a tool wired into its event's matcher
(#171). Before #1646 the registered PreToolUse matchers were
``Bash|Edit|Write``, ``AskUserQuestion``, ``mcp__.*[Ss]lack.*``,
``mcp__glab__glab_mr_.*`` — no ``Agent`` — so both Agent arms were phantom. This
pins that an ``Agent`` matcher now routes to the hook router on PreToolUse, making
the (default-ON, #1733) orchestrator-boundary deny genuinely live.
"""

import json
import re
from pathlib import Path

import pytest

_HOOKS_JSON = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"


def _pretooluse_entries() -> list[dict]:
    config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    return config["hooks"]["PreToolUse"]


def _matcher_routes_to_router(entry: dict) -> bool:
    return any(
        h.get("type") == "command" and "hook_router.py" in h.get("command", "") and "PreToolUse" in h.get("command", "")
        for h in entry.get("hooks", [])
    )


def test_agent_pretooluse_matcher_is_present() -> None:
    matchers = [entry.get("matcher", "") for entry in _pretooluse_entries()]
    agent_entries = [m for m in matchers if m == "Agent" or "Agent" in m.split("|")]
    assert agent_entries, (
        f"no PreToolUse matcher routes the `Agent` tool — the orchestrator-boundary and "
        f"dispatch-quote Agent arms are phantom. Present matchers: {matchers}"
    )


def test_agent_matcher_routes_to_hook_router_pretooluse() -> None:
    entries = _pretooluse_entries()
    agent_entry = next(
        (e for e in entries if e.get("matcher") == "Agent" or "Agent" in e.get("matcher", "").split("|")),
        None,
    )
    assert agent_entry is not None, "an Agent PreToolUse matcher entry must exist"
    assert _matcher_routes_to_router(agent_entry), (
        "the Agent PreToolUse matcher must route to hook_router.py --event PreToolUse"
    )


def test_existing_pretooluse_matchers_preserved() -> None:
    """Every tool the PreToolUse chain already covered must STILL be covered.

    Asserted per TOOL rather than per matcher STRING: widening an alternation (adding
    ``NotebookEdit`` beside ``Edit``/``Write``) adds coverage and must pass, while dropping
    any tool from the chain must fail. Pinning the literal string conflated the two, so a
    pure widening read as a regression.
    """
    matchers = [matcher for entry in _pretooluse_entries() for matcher in entry.get("matcher", "").split("|")]
    for expected in (
        "Bash",
        "Edit",
        "Write",
        "NotebookEdit",
        "AskUserQuestion",
        "mcp__slack__slack_send_message",
        "mcp__glab__glab_mr_create",
    ):
        assert any(re.fullmatch(matcher, expected) for matcher in matchers), (
            f"existing PreToolUse coverage of {expected!r} must be preserved"
        )


def test_task_dispatch_reaches_both_router_events_without_taskcreate() -> None:
    config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    for event in ("PreToolUse", "PostToolUse"):
        routed = [
            entry["matcher"].split("|")
            for entry in config["hooks"][event]
            if any("hook_router.py" in hook.get("command", "") for hook in entry["hooks"])
        ]
        assert any("Task" in tools for tools in routed)
        assert any("Task" in tools and "TaskCreate" not in tools for tools in routed)


def _routes(event: str, tool_name: str) -> bool:
    config = json.loads(_HOOKS_JSON.read_text(encoding="utf-8"))
    return any(
        re.fullmatch(entry["matcher"], tool_name)
        and any(f"hook_router.py --event {event}" in hook.get("command", "") for hook in entry["hooks"])
        for entry in config["hooks"][event]
        if entry.get("matcher") != "*"
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__other__create_ticket",
        "mcp__glab__glab_mr_create",
        "mcp__connector_Notion__notion-update-page",
    ],
)
def test_foreign_mcp_write_reaches_pretool_router(tool_name: str) -> None:
    assert _routes("PreToolUse", tool_name)


def test_foreign_mcp_matcher_covers_arbitrary_servers() -> None:
    matchers = [entry["matcher"] for entry in _pretooluse_entries()]
    assert any(re.fullmatch(matcher, "mcp__unknown__publish") for matcher in matchers)
