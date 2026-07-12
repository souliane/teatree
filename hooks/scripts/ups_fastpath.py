"""Django-free UserPromptSubmit fast path — skip the per-prompt ``django.setup()`` when idle (#22).

Three ``UserPromptSubmit`` handlers booted Django IN-PROCESS on every prompt:
``handle_record_presence`` (a heartbeat FILE write), ``handle_inject_pending_questions``
and ``handle_inject_pending_chat`` (backlog reads). ``django.setup()`` is idempotent, so
the FIRST boot costs the whole ~8s cold UPS tax — paid even when there is nothing to
record beyond the heartbeat and nothing to inject (the common case). This sibling removes
that tax:

:func:`record_presence` writes the heartbeat in pure stdlib — the write never needed
Django (the handler only booted it to *import* the availability module for
``PRESENCE.record``), and the on-disk bytes are identical to ``PresenceHeartbeat.record``.

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

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from hooks.scripts.managed_repo import teatree_src_on_path

# Mirrors ``teatree.core.availability.presence_path`` (``DATA_DIR / <name>``) and
# ``pending_chat_injection`` / ``deferred_question`` ``Meta.db_table``.
_PRESENCE_FILENAME = "availability_presence"

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
    """Stamp the live-presence heartbeat file in pure stdlib (no ``django.setup()``).

    Writes ``{"at": <iso>, "session": <id>}`` (``sort_keys``) + a trailing newline
    atomically (temp file + ``os.replace``) to ``<PRIMARY data dir>/availability_presence``
    — byte-identical to ``PresenceHeartbeat.record``, which ``availability.resolve_mode``
    reads to upgrade a schedule-derived ``away`` to ``present``. Best-effort and silent:
    an unresolvable data dir or any OS error records nothing (the schedule then decides),
    exactly as the handler's prior fail-open ``bootstrap`` path did.
    """
    target = _presence_path()
    if target is None:
        return
    try:
        moment = datetime.now(tz=UTC).isoformat()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(prefix=".presence-", suffix=".tmp", dir=str(target.parent))
        tmp_path = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"at": moment, "session": session_id}, fh, sort_keys=True)
                fh.write("\n")
            tmp_path.replace(target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError:
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


def _presence_path() -> Path | None:
    """``<PRIMARY data dir>/availability_presence``; ``None`` when unresolvable.

    Reuses ``cold_reader``'s DB-home resolution (``canonical_config_db().parent`` is the
    PRIMARY data dir the installed ``t3`` reads/writes, correct even from a worktree), so
    the heartbeat lands where ``availability.presence_path`` expects it.
    """
    try:
        with teatree_src_on_path():
            from teatree.config.cold_reader import canonical_config_db  # noqa: PLC0415 — deferred: cold-hook import

            return canonical_config_db().parent / _PRESENCE_FILENAME
    except Exception:  # noqa: BLE001 — hook crash-proof: unresolvable data dir ⇒ no heartbeat
        return None


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
