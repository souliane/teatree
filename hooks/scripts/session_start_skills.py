"""SessionStart skill injection for an autoloaded session (#3869).

``autoload`` ENGAGES a session, but engagement is not loading. The only skill-selection
path was wired to ``UserPromptSubmit`` and returns early without a prompt, so with
``autoload = true`` the skills arrived only AFTER the first message had been answered —
and a session that never receives a ``UserPromptSubmit`` (a dispatched worker, a session
whose first act is a tool call) got nothing at all. The first turn is usually the one that
sets the approach for the whole session, so "suggested afterwards" is the wrong time.

This module answers the SAME question with the SAME resolver
(``scripts/lib/skill_loader.suggest_skills`` → ``SkillLoadingPolicy.select_for_prompt_hook``)
at the SessionStart moment instead. It is deliberately not a second selection path: a
second answer to "which skills apply" is the #3854 duplication class, and the two answers
would drift. The prompt is passed EMPTY, which is honest rather than lossy — the resolver
uses the prompt only for the loose supplementary keyword regexes, and there is no task
intent to scan before the first turn.

The result is written to ``<session>.pending``, the same demand set the PreToolUse
skill-loading gate reads, so the injection and the enforcement cannot disagree.

Crash-proof: every failure degrades to ``""``. SessionStart also carries loop bootstrap and
the parked hand-off drain, and a skill hint must never be the reason those do not run.
"""

import sys
from pathlib import Path
from typing import Any

# Alias both identities so a bare ``from session_start_skills import ...`` (the live hook,
# whose dir is on ``sys.path``) and ``hooks.scripts.session_start_skills`` (a
# subprocess/test import) resolve the SAME module object — the pattern every sibling uses.
sys.modules.setdefault("session_start_skills", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.session_start_skills", sys.modules[__name__])


def _suggest(loader_input: dict[str, Any]) -> dict[str, Any]:
    """Call the shared selection resolver, with ``scripts/`` on ``sys.path`` for its import.

    Isolated behind its own function so the SessionStart wiring can be exercised without
    the on-disk skill tree, and so the ``sys.path`` mutation is always undone.
    """
    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
    if not (scripts_dir / "lib" / "skill_loader.py").is_file():
        return {}
    sys.path.insert(0, str(scripts_dir))
    try:
        from lib.skill_loader import suggest_skills  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup

        return suggest_skills(loader_input)
    finally:
        sys.path.pop(0)


def session_start_skill_context(session_id: str) -> str:
    """The load-these-skills directive for *session_id*, or ``""`` when there is nothing to say.

    Writes the hard demand set to ``<session>.pending`` as a side effect — the SAME file the
    ``UserPromptSubmit`` path writes and the PreToolUse gate enforces.

    MUST be called BEFORE the statusline skill seed (``engagement.engage(seed_skills=True)``).
    That seed writes the lifecycle-core names into ``<session>.skills``, which is the LOADED
    set this selection subtracts from — running after it would let names that were seeded
    for a statusline segment, never actually loaded, suppress their own injection.

    Returns ``""`` on ANY failure (see the module note): the caller merges this into the one
    SessionStart stdout write, and an empty string simply contributes nothing.
    """
    if not session_id:
        return ""
    try:
        from hooks.scripts.hook_router import (  # noqa: PLC0415 deferred back-import: avoids an import cycle
            _ensure_state_dir,
            _state_file,
            normalize_skill_name,
        )
        from hooks.scripts.skill_loader_input import (  # noqa: PLC0415 — deferred: cold-hook import
            build_skill_loader_input,
        )
        from hooks.scripts.skill_suggestion_render import (  # noqa: PLC0415 — deferred: cold-hook import
            render_skill_suggestion_message,
        )

        _ensure_state_dir()
        result = _suggest(build_skill_loader_input("", session_id))
        if not result:
            return ""
        return render_skill_suggestion_message(
            result,
            pending=_state_file(session_id, "pending"),
            # The ``t3`` CLI reminder is keyed off workspace/infrastructure INTENT in the
            # prompt. There is no prompt here, so there is no intent to key off, and
            # emitting it unconditionally would put a reminder on every single session.
            t3_reminder="",
            normalize=normalize_skill_name,
        )
    except Exception:  # noqa: BLE001 — crash-proof hook: never let a skill hint break SessionStart
        return ""


__all__ = ["session_start_skill_context"]
