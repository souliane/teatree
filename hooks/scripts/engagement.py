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


def engage(session_id: str) -> None:
    """Mark ``session_id`` teatree-active — the one engagement writer.

    An empty ``session_id`` is a no-op (nothing to key the marker on).
    """
    if not session_id:
        return
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    _ensure_state_dir()
    _state_file(session_id, "teatree-active").touch()
