"""Mode-posture read for the bare-``python3`` Stop/PreToolUse hooks (#3826, #2559).

The harness invokes the hooks as a bare ``python3`` with NO ``uv`` env, so Django
cannot be booted in the hook interpreter. This probe answers the only two questions a
hook asks about the operating mode — do questions defer, does the self-pump pause —
in pure stdlib.

It used to answer them from a JSON mirror file under the data dir that the
Django override chokepoint wrote through. That mirror was a second source of truth:
it held a week-old ``autonomous_away`` while the override table was empty, and every
``AskUserQuestion`` was silently captured while the owner sat at the keyboard
(#3826). The mirror is gone. Both predicates now read the SAME control DB the Django
resolver reads, via the Django-free
:func:`teatree.config.cold_mode.resolve_cold_posture` (``src/`` is put on
``sys.path`` for that import by the shared :func:`teatree_src_on_path` bootstrap,
#1314) — so a divergence is not merely detected, it is unrepresentable.

**Fails toward ASKING.** An unimportable ``teatree``, an unreadable DB, or any other
failure resolves to "does not defer, does not pause": the user gets interrupted
rather than silenced. Failing closed to the most restrictive posture is exactly what
muted the owner for a week.

It lives in a bare sibling module (not ``hook_router``) so the over-cap, shrink-only
router gains the stdlib behaviour without growing (``hooks/CLAUDE.md`` § "Adding a
gate").
"""

from hooks.scripts.managed_repo import teatree_src_on_path


def resolved_defers_questions() -> bool:
    """True when the active mode defers ``AskUserQuestion`` to the durable backlog."""
    return _posture()[0]


def resolved_pauses_self_pump() -> bool:
    """True when the active mode parks the Stop self-pump (the holiday posture only)."""
    return _posture()[1]


def _posture() -> tuple[bool, bool]:
    """``(defers_questions, pauses_self_pump)`` from the control DB; ``(False, False)`` on any failure."""
    try:
        with teatree_src_on_path():
            from teatree.config.cold_mode import resolve_cold_posture  # noqa: PLC0415 — deferred: cold-hook import

            posture = resolve_cold_posture()
            return posture.defers_questions, posture.pauses_self_pump
    except Exception:  # noqa: BLE001 — hook crash-proof: an unresolvable posture ASKS, never mutes
        return False, False


__all__ = ["resolved_defers_questions", "resolved_pauses_self_pump"]
