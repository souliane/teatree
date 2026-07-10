"""Tick-level DeferredQuestion → Slack drains over :func:`notify_user`.

The cross-tick re-delivery drains for the durable question backlog, split out
of ``teatree.core.notify`` (at its module-health LOC cap). Both post pending
:class:`DeferredQuestion` rows to the user's DM through the canonical
:func:`teatree.core.notify.notify_user` egress:

* :func:`drain_deferred_questions` — the away→present resurface of the whole
    pending backlog (manual ``questions resurface`` + the auto away→present
    transition);
* :func:`drain_unmirrored_deferred_questions` — the headless ask-loop poster:
    posts rows with no Slack mirror yet and stamps the mirror coordinates so a
    reply can later bind.

The INFO-redelivery peer (``drain_undelivered_notifies``) stays in
``teatree.core.notify`` — a different durability concern (no-backend at post
time vs. no-mirror / away at ask time).
"""

import json
import logging
import os

from teatree.core.models import BotPing, DeferredQuestion
from teatree.core.notify import NotifyKind, notify_user

logger = logging.getLogger(__name__)


def _resurface_text(row: DeferredQuestion) -> str:
    lines = [f"*Pending question #{row.pk}* (deferred while you were away):", row.question]
    try:
        options = json.loads(row.options_json) if row.options_json else []
    except (ValueError, TypeError):
        options = []
    for i, opt in enumerate(options, 1):
        if not isinstance(opt, dict):
            continue
        label = opt.get("label", "")
        desc = opt.get("description", "")
        lines.append(f"  {i}. {label}" + (f" — {desc}" if desc else ""))
    lines.append(f"\n_Answer with_ `t3 teatree questions answer {row.pk} <text>`")
    return "\n".join(lines)


def _scoped_overlay_env(overlay: str) -> str | None:
    """Set ``T3_OVERLAY_NAME`` to *overlay* for the drain; return the prior value to restore."""
    previous = os.environ.get("T3_OVERLAY_NAME")
    if overlay:
        os.environ["T3_OVERLAY_NAME"] = overlay
    return previous


def _restore_overlay_env(overlay: str, previous: str | None) -> None:
    if not overlay:
        return
    if previous is None:
        os.environ.pop("T3_OVERLAY_NAME", None)
    else:
        os.environ["T3_OVERLAY_NAME"] = previous


def drain_deferred_questions(*, user_id: str = "", overlay: str = "") -> tuple[int, int]:
    """Re-post the pending :class:`DeferredQuestion` backlog to the user's Slack DM.

    The single canonical away→present drain. Both the manual
    ``t3 teatree questions resurface`` command and the automatic
    ``write_override(MODE_PRESENT)`` away→present transition call this — one code
    path, no duplicated egress logic.

    Idempotent per question (the ``BotPing`` ledger dedupes the per-question
    ``resurface-deferred-question:<stable-ref>`` key — the row's
    :attr:`~teatree.core.models.deferred_question.DeferredQuestion.stable_notify_ref`,
    never its local pk), so re-running on a later tick or after a manual
    ``resurface`` never double-posts. Fails open: a delivery
    failure for one question is recorded on its ``BotPing`` row by
    :func:`notify_user` and never aborts the drain or raises. Returns
    ``(delivered, total)``.
    """
    rows = list(DeferredQuestion.pending())
    if not rows:
        return 0, 0

    previous_overlay = _scoped_overlay_env(overlay)
    delivered = 0
    try:
        for row in rows:
            if notify_user(
                _resurface_text(row),
                kind=NotifyKind.QUESTION,
                idempotency_key=f"resurface-deferred-question:{row.stable_notify_ref}",
                user_id=user_id or None,
            ):
                delivered += 1
    finally:
        _restore_overlay_env(overlay, previous_overlay)

    return delivered, len(rows)


def drain_unmirrored_deferred_questions(*, user_id: str = "", overlay: str = "") -> tuple[int, int]:
    """Post the un-mirrored :class:`DeferredQuestion` backlog and stamp its mirror.

    The tick-level outbound poster for the headless ask-loop (peer of
    ``drain_undelivered_notifies``): the SDK lane and the orphaned
    ``task_repair._escalate_stall`` rows record a pending question with no
    ``slack_ts`` and nobody posts it. This drain posts each via
    :func:`notify_user` (idempotent under the
    ``mirror-deferred-question:<stable-ref>`` key — the row's
    :attr:`~teatree.core.models.deferred_question.DeferredQuestion.stable_notify_ref`,
    never its local pk) and, on a confirmed send, reads the delivered ``BotPing`` coordinates
    back and stamps ``slack_ts``/``slack_channel`` so the reply scanner can later
    bind a reply (verify-by-re-read). A row that does not deliver (no backend
    resolved in this context) is left un-mirrored — the durable row IS the
    fallback — and retried next tick. Returns ``(mirrored, total)``.
    """
    rows = list(DeferredQuestion.unmirrored_pending())
    if not rows:
        return 0, 0

    previous_overlay = _scoped_overlay_env(overlay)
    mirrored = 0
    try:
        for row in rows:
            key = f"mirror-deferred-question:{row.stable_notify_ref}"
            if not notify_user(
                _resurface_text(row),
                kind=NotifyKind.QUESTION,
                idempotency_key=key,
                user_id=user_id or None,
            ):
                continue
            ping = BotPing.objects.filter(idempotency_key=key, status=BotPing.Status.SENT).first()
            if ping and ping.posted_ts and row.mark_mirrored(channel=ping.channel_ref, slack_ts=ping.posted_ts):
                mirrored += 1
    finally:
        _restore_overlay_env(overlay, previous_overlay)

    return mirrored, len(rows)
