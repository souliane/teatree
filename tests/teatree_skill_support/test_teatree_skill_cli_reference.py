"""Anti-drift test for the teatree skill's CLI Reference command list.

``skills/teatree/SKILL.md`` § "CLI Reference" lists the top-level (no-overlay)
``t3`` commands. That list drifted: it claimed ~8 of the ~29 registered
top-level commands and nothing guarded against a claim going stale. This test
asserts every top-level command the skill claims is actually registered on the
``t3`` typer app — so the skill cannot silently name a command that does not
exist (or has been renamed).

The direction is a SUBSET check: the skill is allowed to highlight a curated
slice of the commands, but every name it does highlight must be real.
"""

from __future__ import annotations  # noqa: TID251 — pure-logic doc-invariant test

import re
from pathlib import Path

from teatree.cli import app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEATREE_SKILL = _REPO_ROOT / "skills" / "teatree" / "SKILL.md"


def _registered_top_level_commands() -> set[str]:
    """Every top-level command/group name registered on the ``t3`` app."""
    names: set[str] = set()
    for command in app.registered_commands:
        name = command.name or (command.callback.__name__.replace("_", "-") if command.callback else None)
        if name:
            names.add(name)
    for group in app.registered_groups:
        if group.name:
            names.add(group.name)
    return names


def _claimed_top_level_commands() -> set[str]:
    """Parse the top-level commands the skill's CLI Reference sentence claims.

    The list lives in the "Top-level commands (no overlay needed):" sentence as
    a run of inline-code ``t3 <command>`` references. Overlay-scoped examples
    (``t3 <overlay> …``) are deliberately excluded — they are not top-level.
    """
    text = _TEATREE_SKILL.read_text(encoding="utf-8")
    marker = "Top-level commands (no overlay needed):"
    start = text.index(marker)
    # The claim is a single sentence/line ending at the first newline.
    line = text[start : text.index("\n", start)]
    # Match ``t3 <command>`` inside inline code spans; the command is the first
    # token after ``t3`` and is never the ``<overlay>`` placeholder.
    claimed = set(re.findall(r"`t3 ([a-z][a-z0-9-]+)`", line))
    return {c for c in claimed if c != "overlay"}


def test_claimed_commands_are_a_subset_of_registered() -> None:
    claimed = _claimed_top_level_commands()
    registered = _registered_top_level_commands()
    assert claimed, "no top-level commands parsed from the teatree skill CLI Reference"
    unknown = claimed - registered
    assert not unknown, (
        f"teatree SKILL.md CLI Reference claims top-level commands that are not "
        f"registered on the t3 app: {sorted(unknown)}. Registered: {sorted(registered)}"
    )


def test_skill_claims_a_representative_set() -> None:
    # Guard against the list silently shrinking back to a tiny slice: the skill
    # should name a meaningful fraction of the real top-level surface, not ~8.
    claimed = _claimed_top_level_commands()
    assert len(claimed) >= 15, (
        f"teatree SKILL.md CLI Reference names only {len(claimed)} top-level commands; "
        "list the real top-level surface so it stays useful and self-contained"
    )
