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
    reply can later bind;
* :func:`resurface_question_backlog` — the RECURRING nag (directive #36). The
    two drains above are single-shot per question (each keys its idempotency on
    the row's ``stable_notify_ref``), so a question the owner never answers is
    never raised again. This one re-raises the whole backlog as ONE digest per
    :data:`RESURFACE_INTERVAL_HOURS` bucket in the same DM thread — the lowest
    message count that still keeps an unanswered question visible.

The INFO-redelivery peer (``drain_undelivered_notifies``) stays in
``teatree.core.notify`` — a different durability concern (no-backend at post
time vs. no-mirror / away at ask time).
"""

import datetime as dt
import json
import logging
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing, DeferredQuestion
from teatree.core.notify import NotifyKind, notify_user

# How many deferred questions one tick may mirror. The backlog accumulates silently
# while the owner is away, so an unbounded drain delivers it all in one burst the
# moment they return — the owner reads that as spam and mutes the channel, which
# costs the next genuine question too. Nothing is dropped: the remainder is re-read
# on the next tick, so a question that already waited hours waits one more cadence.
_MAX_MIRRORS_PER_TICK = 3

#: Prefix of the ``BotPing`` idempotency key :func:`drain_deferred_questions` writes.
#: The cap must bound NEW deliveries, so the selection reads this ledger back and skips
#: what a fresh send would not re-deliver — a slice taken straight off the oldest-first
#: queue would re-select the same settled head every call and never advance (#4064).
_RESURFACE_KEY_PREFIX = "resurface-deferred-question:"


def _refs_the_send_will_not_redeliver() -> set[str]:
    """Every ``stable_notify_ref`` a fresh send would decline to re-deliver.

    Read from the ``BotPing`` ledger rather than tracked on the question row: the
    ledger is what :func:`notify_user` dedupes against, so reading it back is what
    makes the selection agree with the send instead of racing it.

    The predicate is the complement of :meth:`~teatree.core.models.BotPing.redeliverable_q`
    — every status whose claim returns ``ALREADY_SENT`` or ``IN_FLIGHT``, not ``SENT``
    alone. A SENT_UNVERIFIED, EXPIRED, LOGGED or fresh-SENDING row is one the send path
    stands down on, so leaving it in the window lets it hold a cap slot forever with
    nothing to free it. FAILED, NOOP and a stale SENDING claim stay in the window because
    the send path really does re-deliver them; counting those as handled would skip the
    question permanently.
    """
    keys = (
        BotPing.objects.filter(idempotency_key__startswith=_RESURFACE_KEY_PREFIX)
        .exclude(BotPing.redeliverable_q())
        .values_list("idempotency_key", flat=True)
    )
    return {key.removeprefix(_RESURFACE_KEY_PREFIX) for key in keys}


if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)

#: How long one backlog-digest bucket lasts. The digest's idempotency key is the
#: bucket index, so the ``BotPing`` ledger collapses every tick inside a bucket to
#: a single delivered message and a new bucket is the next nag.
RESURFACE_INTERVAL_HOURS = 24

#: How many questions the digest names before it degrades to a "+N more" count —
#: a nag the owner can read on a phone, not a wall of 42 lines.
_DIGEST_LIST_CAP = 10
_DIGEST_QUESTION_CHARS = 90


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
    lines.append("\n_Reply in this thread — a typed reply is what gets recorded as your answer._")
    return "\n".join(lines)


def _escalation_note(row: DeferredQuestion, *, now: dt.datetime) -> str:
    """How long an escalated row has waited, and how often the backstop has said so.

    The rendering IS the age backstop's only owner-visible effect: the stamp records
    that a row sat past the ceiling without resolving it, so a digest that read the
    same before and after would make "escalated" indistinguishable from "ignored".
    """
    if row.escalated_at is None:
        return ""
    return f"  _(unanswered {(now - row.created_at).days}d, escalated {row.escalation_count}x)_"


def _digest_line(row: DeferredQuestion, *, now: dt.datetime) -> str:
    first_line = row.question.strip().splitlines()[0] if row.question.strip() else ""
    if len(first_line) > _DIGEST_QUESTION_CHARS:
        first_line = first_line[: _DIGEST_QUESTION_CHARS - 1].rstrip() + "…"
    return f"  • #{row.pk} — {first_line}{_escalation_note(row, now=now)}"


def format_backlog_digest(rows: Sequence[DeferredQuestion], *, now: dt.datetime | None = None) -> str:
    """The single recurring nag message covering *rows*.

    One header, up to :data:`_DIGEST_LIST_CAP` question lines, and a remainder
    count — so a 42-deep backlog is one readable message rather than 42 DMs. The
    ``#<id>`` prefix is what the owner echoes back, since a bare reply in a shared
    DM thread cannot say WHICH question it answers.

    Escalated rows are listed FIRST (stably, so the rest keep their order): they are
    the ones that have waited longest without an answer, and so exactly the ones the
    cap would otherwise drop out of the message.
    """
    stamped_at = now or timezone.now()
    escalated = sum(1 for row in rows if row.escalated_at is not None)
    header = f"*{len(rows)} open question{'s' if len(rows) != 1 else ''} waiting on you.*"
    if escalated:
        header += f" *{escalated} past the age ceiling.*"
    lines = [header, "Reply in this thread as `#<id> <your answer>`."]
    ordered = sorted(rows, key=lambda row: row.escalated_at is None)
    lines.extend(_digest_line(row, now=stamped_at) for row in ordered[:_DIGEST_LIST_CAP])
    remainder = len(rows) - _DIGEST_LIST_CAP
    if remainder > 0:
        lines.append(f"  +{remainder} more.")
    return "\n".join(lines)


def resurface_question_backlog(
    *,
    user_id: str = "",
    overlay: str = "",
    backend: "MessagingBackend | None" = None,
    now: dt.datetime | None = None,
) -> tuple[bool, int]:
    """Post at most one backlog digest per interval; return ``(posted, pending)``.

    Directive #36's recurring half: an unanswered question is re-raised until the
    owner reacts, in the same DM thread (:func:`notify_user` threads into the
    active DM thread) and never as a per-question burst. The interval bucket IS
    the idempotency key, so every tick inside one bucket collapses onto the single
    delivered ``BotPing`` row and only a new bucket posts again. INTERNAL-audience
    rows (an agent's own tool-lack self-report) are excluded, as in the two
    first-post drains.
    """
    rows = [r for r in DeferredQuestion.pending() if r.audience != DeferredQuestion.Audience.INTERNAL]
    if not rows:
        return False, 0

    stamped_at = now or timezone.now()
    bucket = int(stamped_at.timestamp()) // (RESURFACE_INTERVAL_HOURS * 3600)
    previous_overlay = _scoped_overlay_env(overlay)
    try:
        posted = notify_user(
            format_backlog_digest(rows, now=stamped_at),
            kind=NotifyKind.QUESTION,
            idempotency_key=f"question-backlog-digest:{bucket}",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id=user_id or None,
        )
    finally:
        _restore_overlay_env(overlay, previous_overlay)
    return posted, len(rows)


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
    ``resurface`` never double-posts. The cap is applied AFTER that ledger is read
    back, so it bounds NEW deliveries and the window advances every call until the
    backlog is drained. **Capped per call** for the same reason the
    mirror drain is: an away→present transition after a long absence would otherwise
    deliver the whole accumulated backlog as one burst of DMs. Returning to present is
    precisely when the backlog is largest, so this is the path that actually fires.
    Fails open: a delivery
    failure for one question is recorded on its ``BotPing`` row by
    :func:`notify_user` and never aborts the drain or raises. Returns
    ``(delivered, total)``.
    """
    # DM only owner-audience rows; INTERNAL escalations (repair-loop / dispatch
    # health the box raised about itself) stay logged/statusline-only.
    owner_rows = [r for r in DeferredQuestion.pending() if r.audience != DeferredQuestion.Audience.INTERNAL]
    # Skip what a fresh send would decline to re-deliver BEFORE applying the cap. The queue
    # is oldest-first, so capping it directly hands the same stood-down head back every
    # time: each call deduped to a no-op and every row behind it stayed unreachable, on a
    # tick, on an away->present transition and on a manual resurface alike (#4064).
    settled = _refs_the_send_will_not_redeliver()
    rows = [r for r in owner_rows if r.stable_notify_ref not in settled][:_MAX_MIRRORS_PER_TICK]
    if not rows:
        # Nothing NEW to send. The backlog total is still reported so a caller cannot read
        # "0 delivered" as "queue empty" — the distinction this drain previously erased.
        return 0, len(owner_rows)

    previous_overlay = _scoped_overlay_env(overlay)
    delivered = 0
    try:
        for row in rows:
            if notify_user(
                _resurface_text(row),
                kind=NotifyKind.QUESTION,
                idempotency_key=f"resurface-deferred-question:{row.stable_notify_ref}",
                audience=NotifyAudience.OWNER_QUESTION,
                user_id=user_id or None,
            ):
                delivered += 1
    finally:
        _restore_overlay_env(overlay, previous_overlay)

    # The denominator is the BACKLOG, never the capped slice: `3/3` read as "all pending
    # delivered" while 69 were outstanding, which is how a permanently-stalled queue
    # looked healthy (#4064).
    return delivered, len(owner_rows)


def drain_unmirrored_deferred_questions(
    *, user_id: str = "", overlay: str = "", backend: "MessagingBackend | None" = None
) -> tuple[int, int]:
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

    **The batch is CAPPED per tick.** The backlog grows silently whenever the
    owner is away (an `unattended` preset defers every question), so draining it
    unbounded turns the moment they come back into one DM per accumulated
    question, seconds apart — 52 in one burst is what this fix was written for.
    The owner reads that as spam and mutes the channel, which loses the next
    genuine question too. The remainder is not dropped: `unmirrored_pending()`
    is re-read every tick, so the backlog drains steadily instead of at once,
    and a question that has waited hours can wait one more cadence.
    """
    rows = list(DeferredQuestion.unmirrored_pending())[:_MAX_MIRRORS_PER_TICK]
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
                audience=NotifyAudience.OWNER_QUESTION,
                backend=backend,
                user_id=user_id or None,
            ):
                continue
            ping = BotPing.objects.filter(idempotency_key=key, status=BotPing.Status.SENT).first()
            if ping and ping.posted_ts and row.mark_mirrored(channel=ping.channel_ref, slack_ts=ping.posted_ts):
                mirrored += 1
    finally:
        _restore_overlay_env(overlay, previous_overlay)

    return mirrored, len(rows)
