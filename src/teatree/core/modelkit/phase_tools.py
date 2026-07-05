"""Per-phase tool least-privilege table — one source of truth for both lanes.

The canonical phase vocabulary (:mod:`teatree.core.modelkit.phases`) says WHICH
sub-agent runs a ``(role, phase)`` pair; this module says WHICH tools that phase
may call. It is the single source of truth consumed by BOTH runtime lanes: Lane
B (``pydantic_ai``, PR-03) filters its assembled toolsets down to
:func:`tools_for_phase` — a phase only sees the tools it is allowed; Lane A
(``claude_sdk``, PR-11) injects the COMPLEMENT
(:func:`disallowed_tools_for_phase`) as ``ClaudeAgentOptions.disallowed_tools``
so the same least-privilege holds on the SDK transport.

The names here are teatree's OWN capability tool names (the Lane-B
``FunctionToolset`` tool names in :mod:`teatree.agents.lane_b`), which are the
provider-neutral vocabulary; Lane A maps each to its SDK-native equivalent at
its own boundary. ``normalize_phase`` collapses spellings so a table keyed on the
canonical token resolves a task stored with any accepted alias.
"""

from typing import Final

from teatree.core.modelkit.phases import normalize_phase

#: Every capability tool name Lane B can expose. A phase's allowance is a subset;
#: the complement (universe minus allowance) is the disallow list Lane A injects.
ALL_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "shell",
        "web_fetch",
        "web_search",
        "dispatch_subtask",
        "recall_memory",
        "record_attempt",
    }
)

# Reusable capability bundles, composed into per-phase allowances below.
_READ_ONLY: Final[frozenset[str]] = frozenset({"read_file", "search_files", "recall_memory"})
_WEB: Final[frozenset[str]] = frozenset({"web_fetch", "web_search"})
_WRITE: Final[frozenset[str]] = frozenset({"write_file", "edit_file"})
_FULL: Final[frozenset[str]] = ALL_TOOLS

#: Canonical phase -> the exact set of capability tool names it may call.
#: A read-only phase (reviewing, e2e_reviewing, requesting_review, scanning_news,
#: answering, codex_reviewing) has NO write/edit/shell — the cold-review
#: least-privilege PR-11 enforces on Lane A. A write phase (coding, testing, e2e,
#: debugging) gets the full set. ``bughunt`` executes to reproduce a candidate
#: but never writes (shell + dispatch, no write/edit). An unknown phase falls
#: back to read-only (:func:`tools_for_phase`) — deny-by-default, so a new phase
#: never silently inherits shell/write until it is added here. TOTALITY: every
#: dispatchable ``SUBAGENT_BY_PHASE`` phase MUST have an explicit entry here (the
#: ``test_registry_parity`` totality lane), so the read-only fallback is
#: defense-in-depth for a genuinely unregistered phase, never the silent
#: resolution for a dispatchable one (#10).
_TOOLS_BY_PHASE: Final[dict[str, frozenset[str]]] = {
    "planning": _READ_ONLY | _WEB | {"dispatch_subtask"},
    "scoping": _READ_ONLY | _WEB,
    "coding": _FULL,
    "testing": _FULL,
    "e2e": _FULL,
    "debugging": _FULL,
    "reviewing": _READ_ONLY | _WEB,
    "e2e_reviewing": _READ_ONLY | _WEB,
    "codex_reviewing": _READ_ONLY | _WEB,
    "codex_adversarial_reviewing": _READ_ONLY | _WEB,
    "requesting_review": _READ_ONLY,
    "scanning_news": _READ_ONLY | _WEB,
    "critic_reviewing": _READ_ONLY | _WEB,
    # North-star PR-6 directive interpreter: read-only + codebase search only — it
    # finds the real core seam and drafts a sketch, never edits or shells out.
    "directive_interpreting": _READ_ONLY | _WEB,
    "bughunt": _READ_ONLY | {"shell", "dispatch_subtask"},
    "shipping": _READ_ONLY | {"shell", "record_attempt"},
    "answering": _READ_ONLY | _WEB,
    "retro": _READ_ONLY | _WRITE,
}


def tools_for_phase(phase: str) -> frozenset[str]:
    """Return the capability tool names *phase* may call.

    ``phase`` is normalized so a short-verb spelling resolves the same as the
    canonical gerund. An unknown phase falls back to the read-only bundle —
    deny-by-default, never the full set — so a phase added to the FSM without a
    table entry cannot silently acquire shell/write access.
    """
    return _TOOLS_BY_PHASE.get(normalize_phase(phase), _READ_ONLY)


def disallowed_tools_for_phase(phase: str) -> frozenset[str]:
    """Return the complement — the tools *phase* may NOT call (Lane A injects this)."""
    return ALL_TOOLS - tools_for_phase(phase)
