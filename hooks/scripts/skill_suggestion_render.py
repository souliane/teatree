"""Render the SessionStart skill-suggestion message (#2384 router split, #53).

Two demand tiers. The HARD tier is ``suggestions`` — written to ``<session>.pending``
and load-first enforced by the PreToolUse gate. The SOFT tier is ``companions`` —
surfaced as an optional, complementary suggestion, never written to pending and never
enforced (the counterpart to the hard ``requires`` -> ``suggestions`` edge).

Cold-import safe: stdlib only, no Django / ``teatree`` at import.
"""

from collections.abc import Callable
from pathlib import Path


def companion_suggestion_line(companions: list[str]) -> str:
    """The soft, optional companion-skill line — surfaced, never a hard load demand."""
    if not companions:
        return ""
    names = ", ".join(f"/{c}" for c in companions)
    return f"Suggested companions (optional, complementary — not required): {names}."


def render_skill_suggestion_message(
    result: dict,
    *,
    pending: Path,
    normalize: Callable[[str], str],
) -> str:
    """Persist the HARD demand set and return the message to print.

    Writes ``suggestions`` (the load-first demand set) to
    *pending*, then returns the message: the mandatory LOAD directive and the soft
    companion line, each omitted when empty. With no hard suggestions the message
    is just the companion line (a companion of an already-loaded skill still surfaces).

    The LOAD directive names each skill through the SAME *normalize* the
    *pending* write uses, so the token the agent is told to load is the token
    the PreToolUse gate demands. An overlay's ``skill_path`` arrives here in its
    path shape (``skills/<name>/SKILL.md``) — unloadable as a slash command, and
    not what lands in *pending* — so rendering it raw asked for a skill that
    could never satisfy the gate.
    """
    suggestions = result.get("suggestions", [])
    companion_line = companion_suggestion_line(result.get("companions", []))

    if not suggestions:
        return companion_line

    pending.write_text("\n".join(normalize(skill) for skill in suggestions) + "\n", encoding="utf-8")
    skill_list = ", ".join(f"/{normalize(skill)}" for skill in suggestions)
    parts = [f"LOAD THESE SKILLS NOW (call the Skill tool for each, before doing anything else): {skill_list}."]
    if companion_line:
        parts.append(companion_line)
    return "\n".join(parts)
