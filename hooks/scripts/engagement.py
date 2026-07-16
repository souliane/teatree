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
"""

# The lifecycle-core skill set seeded into ``<session>.skills`` when an
# autoloaded session engages (#3273) — the smaller meaningful set the owner
# expects to see, so the statusline skills segment is never blank on an engaged
# session. A subsequent real Skill/InstructionsLoaded load still augments it.
LIFECYCLE_SEED_SKILLS = ("t3:code", "t3:debug", "t3:test", "t3:ship", "t3:review", "t3:ticket")


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
