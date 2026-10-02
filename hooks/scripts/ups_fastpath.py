"""Django-free UserPromptSubmit fast path — skip the per-prompt ``django.setup()`` when idle (#22).

Three ``UserPromptSubmit`` handlers booted Django IN-PROCESS on every prompt:
``handle_record_presence`` (a heartbeat FILE write), ``handle_inject_pending_questions``
and ``handle_inject_pending_chat`` (backlog reads). ``django.setup()`` is idempotent, so
the FIRST boot costs the whole ~8s cold UPS tax — paid even when there is nothing to
record beyond the heartbeat and nothing to inject (the common case). This sibling removes
that tax:

:func:`record_presence` writes the heartbeat with NO Django — the write never needed it.
It calls ``PresenceHeartbeat.record`` itself: :mod:`teatree.live_presence` is a
foundation leaf importing only the standard library plus :mod:`teatree.paths`, so the
bare hook shares the one implementation instead of re-deriving the on-disk format
(which is how two writers of one artifact start to disagree).

:func:`has_pending_question_work` and :func:`has_pending_chat_work` are Django-free
``sqlite3`` existence probes (via ``teatree.config.cold_reader``) that let the inject
handlers return BEFORE ``django.setup()`` when the backlog is empty. They FAIL OPEN
(assume work) on any unreadable-DB error, so a pending row is never dropped: the handler
still boots Django and the real ORM query decides, so behaviour is unchanged.

Both the presence path and the probes resolve the PRIMARY data dir / DB via
``cold_reader`` (``canonical_config_db``), so a worktree hook reads/writes the same files
the installed ``t3`` does; ``src/`` is bootstrapped onto ``sys.path`` for those imports via
the shared :func:`teatree_src_on_path` (#1314). A bare sibling of ``hook_router`` so the
over-cap, shrink-only router gains the behaviour without growing (``hooks/CLAUDE.md``).
"""

from hooks.scripts.managed_repo import teatree_src_on_path

# DeferredQuestion needs handling when a row is answered-but-not-applied (the apply
# leg) OR still pending (unanswered + not dismissed — the backlog leg). Mirrors
# ``DeferredQuestion.answered_not_applied`` + ``DeferredQuestion.pending``.
_DEFERRED_QUESTION_WORK_SQL = (
    "SELECT 1 FROM teatree_deferred_question "
    "WHERE (answered_at IS NOT NULL AND applied_at IS NULL) "
    "OR (answered_at IS NULL AND dismissed_at IS NULL) "
    "LIMIT 1"
)

# PendingChatInjection needs draining when a row is unconsumed. Mirrors
# ``PendingChatInjection.pending`` (the unscoped ``consumed_at IS NULL``).
_PENDING_CHAT_WORK_SQL = "SELECT 1 FROM teatree_pending_chat_injection WHERE consumed_at IS NULL LIMIT 1"


def record_presence(session_id: str) -> None:
    """Stamp the live-presence heartbeat with no ``django.setup()``.

    Calls the shared :meth:`teatree.live_presence.PresenceHeartbeat.record` — a
    foundation leaf needing only the stdlib and :mod:`teatree.paths`, reachable from
    the bare hook through the same ``src/`` bootstrap the cold reader uses. The
    resolver reads the same file through the same class, so there is exactly one
    definition of where the heartbeat lives and what it contains. Best-effort and
    silent: an unresolvable data dir or any OS error records nothing (the schedule
    then decides), exactly as the handler's prior fail-open ``bootstrap`` path did.
    """
    try:
        with teatree_src_on_path():
            from teatree.live_presence import PRESENCE  # noqa: PLC0415 — deferred: cold-hook import

            PRESENCE.record(session_id=session_id)
    except Exception:  # noqa: BLE001 — hook crash-proof: an unrecordable heartbeat leaves the schedule deciding
        return


def has_pending_question_work() -> bool:
    """True when a ``DeferredQuestion`` row needs applying or is still pending.

    The Django-free pre-check for ``handle_inject_pending_questions``: when this is
    ``False`` the handler returns before ``django.setup()``. FAIL OPEN (``True``) on any
    unreadable-DB error so an injectable/answered row is never dropped — the handler then
    boots Django and the ORM decides.
    """
    return _row_exists(_DEFERRED_QUESTION_WORK_SQL)


def has_pending_chat_work() -> bool:
    """True when an unconsumed ``PendingChatInjection`` row exists.

    The Django-free pre-check for ``handle_inject_pending_chat``: when this is ``False``
    the handler returns before ``django.setup()``. FAIL OPEN (``True``) on any
    unreadable-DB error so a queued Slack reply is never dropped.
    """
    return _row_exists(_PENDING_CHAT_WORK_SQL)


def _row_exists(query: str) -> bool:
    """``cold_reader.row_exists(query, on_error=True)``; ``True`` when the probe cannot run.

    Fails OPEN so an unresolvable reader / unreadable DB never skips a handler that had
    work to do — the handler boots Django and the ORM query decides, unchanged.
    """
    try:
        with teatree_src_on_path():
            from teatree.config.cold_reader import row_exists  # noqa: PLC0415 — deferred: cold-hook import

            return row_exists(query, on_error=True)
    except Exception:  # noqa: BLE001 — can't probe ⇒ assume work ⇒ let the handler boot Django
        return True


__all__ = ["has_pending_chat_work", "has_pending_question_work", "record_presence"]
