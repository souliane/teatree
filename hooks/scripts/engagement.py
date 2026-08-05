"""The single teatree session-engagement seam (#256, autonomous-lane redesign §6/§8.1).

Both engagement paths write the ``.teatree-active`` marker through this ONE
routine — the SessionStart auto-load path (``handle_session_start_bootstrap``
when ``autoload`` is on) and the manual skill-load path
(``handle_track_skill_usage`` when a teatree-requiring skill loads). Before
this seam the marker had two parallel writers reconciled only by a shared read
predicate; that drift (two writers, one reader) is what this collapses, so
auto-loading does exactly what manual engagement does.

A bare sibling of ``hook_router`` (the shrink-only god-module owns the state
directory), it back-imports the router's marker helpers lazily so a test
monkeypatching ``router.STATE_DIR`` is honoured and there is no import cycle at
module top.

Engagement also decides what an engaged session must HOLD, not only what it may
suggest: :func:`autoload_skill_demand` is the hard-demand half of the same seam.
"""

from collections.abc import Iterable

from hooks.scripts.session_lane import LANE_SDK, session_lane
from hooks.scripts.teatree_settings import autoload_enabled

# The lifecycle-core skill set seeded into ``<session>.skills`` when an
# autoloaded session engages (#3273) — the smaller meaningful set the owner
# expects to see, so the statusline skills segment is never blank on an engaged
# session. A subsequent real Skill/InstructionsLoaded load still augments it.
LIFECYCLE_SEED_SKILLS = ("t3:code", "t3:debug", "t3:test", "t3:ship", "t3:review", "t3:ticket")

# The platform skill an ``autoload``-engaged session must hold: the engagement
# skill itself, so holding it and being engaged stay one fact rather than two
# that can drift. Written bare — the pending writer canonicalizes it up to the
# plugin namespace, which is the token the agent loads and the token the
# PreToolUse gate reads back.
PLATFORM_SKILL = "interactive"


def engage(session_id: str, *, seed_skills: bool = False) -> None:
    """Mark ``session_id`` teatree-active — the one engagement writer.

    An empty ``session_id`` is a no-op (nothing to key the marker on). When
    ``seed_skills`` is set (the autoloaded SessionStart path), also seed the
    lifecycle-core skills so the statusline's skills segment is populated from
    the first render instead of staying blank until a manual ``/t3:`` load.
    """
    if not session_id:
        return
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    _ensure_state_dir()
    _state_file(session_id, "teatree-active").touch()
    if seed_skills:
        _seed_lifecycle_skills(session_id)


def autoload_skill_demand(loaded_skills: Iterable[str]) -> list[str]:
    """``[PLATFORM_SKILL]`` when ``autoload`` engaged this ATTENDED session and it is not held yet.

    ``autoload`` is the owner's standing "teatree is on for every session"
    opt-in, so the platform skill is a HARD demand on such a session rather than
    a suggestion the agent may skip. Engagement by itself only armed the
    suggester and the loop; nothing put the skill in front of the agent, so the
    owner-visible contract was implemented nowhere.

    Scoped to the ``autoload`` tier deliberately. A session engaged by loading a
    lifecycle skill (``.t3-engaged``) made no such standing request, and forcing
    the platform skill on it would be an over-block.

    Scoped to the ATTENDED lanes for the same reason. The platform skill is
    Claude Code harness wiring plus attended-session hygiene, and the demand is
    enforced by a ``PreToolUse`` gate that refuses every ``Edit``/``Write``/
    ``Bash`` until it loads — so on an SDK worker the owner's standing opt-in
    lands as a hard block on the factory it was meant to run. Only a POSITIVELY
    identified SDK lane is withheld from: an unknown lane keeps today's demand,
    because this must never silently disengage an attended session whose env
    teatree simply does not recognise. That is the mirror of the sibling
    :func:`headless_authoring_gate.handle_block_interactive_authoring`, which
    refuses only a positively identified interactive lane — both resolve an
    unreadable signal toward leaving the factory alone.

    Every spelling ``<session>.skills`` can carry — bare, namespaced, or an
    overlay's path-shaped ``skill_path`` — canonicalizes to one token, so a
    session that already holds the skill is never asked for it again.
    """
    if not autoload_enabled() or session_lane() == LANE_SDK:
        return []
    from hooks.scripts.hook_router import normalize_skill_name  # noqa: PLC0415 deferred back-import

    wanted = normalize_skill_name(PLATFORM_SKILL)
    held = {normalize_skill_name(skill) for skill in loaded_skills}
    return [] if wanted in held else [PLATFORM_SKILL]


def _seed_lifecycle_skills(session_id: str) -> None:
    """Append the lifecycle-core skills to ``<session>.skills``, deduped, never clobbering."""
    from hooks.scripts.hook_router import (  # noqa: PLC0415 deferred back-import
        _append_line,
        _read_lines,
        _state_file,
        normalize_skill_name,
    )

    skills_file = _state_file(session_id, "skills")
    existing = {normalize_skill_name(s) for s in _read_lines(skills_file)}
    for skill in LIFECYCLE_SEED_SKILLS:
        name = normalize_skill_name(skill)
        if name and name not in existing:
            existing.add(name)
            _append_line(skills_file, name)
