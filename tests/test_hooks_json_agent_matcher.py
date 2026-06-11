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
from pathlib import Path

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
    matchers = {entry.get("matcher", "") for entry in _pretooluse_entries()}
    for expected in ("Bash|Edit|Write", "AskUserQuestion", "mcp__.*[Ss]lack.*", "mcp__glab__glab_mr_.*"):
        assert expected in matchers, f"existing PreToolUse matcher {expected!r} must be preserved"
