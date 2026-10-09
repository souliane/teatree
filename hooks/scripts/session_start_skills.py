"""SessionStart skill injection for an autoloaded session (#3869).

``autoload`` ENGAGES a session, but engagement is not loading: the skills must arrive
before the first turn, which usually sets the approach for the whole session. This is the
one skill-selection path (``scripts/lib/skill_loader.suggest_skills``).

The result is written to ``<session>.pending``, the same demand set the PreToolUse
skill-loading gate reads, so the injection and the enforcement cannot disagree.

Crash-proof: every failure degrades to ``""``; a shadowed apm pin is still named. SessionStart also
carries loop bootstrap and the parked hand-off drain, and a skill hint must never be the reason those
do not run.
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


def _shadowed_pin_warning(exc: BaseException | None) -> str:
    """A shadowed apm pin is a misconfiguration the session must see; any other suggester failure stays silent."""
    pin_shadow = sys.modules.get("teatree.skill_support.pin_shadow")
    if pin_shadow is None or not isinstance(exc, pin_shadow.SkillPinRefusalError):
        return ""
    return f"WARNING: {exc}"


def session_start_skill_context(session_id: str) -> str:
    """The load-these-skills directive for *session_id*, or ``""`` when there is nothing to say.

    Writes the hard demand set to ``<session>.pending`` as a side effect — the SAME file the
    PreToolUse gate enforces.

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
        from hooks.scripts.engagement import autoload_skill_demand  # noqa: PLC0415 — deferred: cold-hook import
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
        loader_input = build_skill_loader_input(session_id)
        warning = ""
        try:
            result = _suggest(loader_input)
        except Exception:  # noqa: BLE001 — the autoload demand must not depend on the suggester surviving
            result = {}
            warning = _shadowed_pin_warning(sys.exception())
        result["suggestions"] = [*autoload_skill_demand(loader_input["loaded_skills"]), *result.get("suggestions", [])]
        message = render_skill_suggestion_message(
            result,
            pending=_state_file(session_id, "pending"),
            normalize=normalize_skill_name,
        )
        return "\n".join(part for part in (warning, message) if part)
    except Exception:  # noqa: BLE001 — crash-proof hook: never let a skill hint break SessionStart
        return ""


__all__ = ["session_start_skill_context"]
