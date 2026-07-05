"""Render the UserPromptSubmit skill-suggestion message (#2384 router split, #53).

Extracted from ``hook_router.handle_user_prompt_submit`` so the shrink-only
router stays under its ratchet while the companion soft-suggestion line is
added here. Two demand tiers. The HARD tier is ``suggestions`` minus
``advisory`` — written to ``<session>.pending`` and load-first enforced by the
PreToolUse gate. The SOFT tier is ``companions`` — surfaced as an optional,
complementary suggestion, never written to pending and never enforced (the
counterpart to the hard ``requires`` -> ``suggestions`` edge).

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
    t3_reminder: str,
    normalize: Callable[[str], str],
) -> str:
    """Persist the HARD demand set and return the UserPromptSubmit message to print.

    Writes the non-advisory ``suggestions`` (the load-first demand set) to
    *pending*, then returns the message: the mandatory LOAD directive, the soft
    companion line, and the *t3_reminder* — each omitted when empty. With no
    hard suggestions the message is just the companion line and reminder (a
    companion of an already-loaded skill still surfaces).
    """
    suggestions = result.get("suggestions", [])
    advisory = set(result.get("advisory", []))
    companion_line = companion_suggestion_line(result.get("companions", []))

    if not suggestions:
        return "\n".join(part for part in (companion_line, t3_reminder) if part)

    demanded = [skill for skill in suggestions if skill not in advisory]
    pending.write_text("\n".join(normalize(skill) for skill in demanded) + "\n", encoding="utf-8")
    skill_list = ", ".join(f"/{skill}" for skill in suggestions)
    parts = [f"LOAD THESE SKILLS NOW (call the Skill tool for each, before doing anything else): {skill_list}."]
    if companion_line:
        parts.append(companion_line)
    if t3_reminder:
        parts.append(t3_reminder)
    return "\n".join(parts)
