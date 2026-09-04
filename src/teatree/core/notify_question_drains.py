"""Tick-level DeferredQuestion → Slack drains over :func:`notify_user`.

The cross-tick re-delivery drains for the durable question backlog, split out
of ``teatree.core.notify`` (at its module-health LOC cap). Both post pending
:class:`DeferredQuestion` rows to the user's DM through the canonical
:func:`teatree.core.notify.notify_user` egress:

* :func:`drain_deferred_questions` — the away→present resurface of the whole
    pending backlog (manual ``questions resurface`` + the auto away→present
    transition);
* :func:`drain_unmirrored_deferred_questions` — the headless ask-loop poster:
    posts rows with no Slack mirror yet AT THE DM ROOT (never nested under the
    owner's active thread, so the stamped ts is one a reply can name) and stamps
    the mirror coordinates so a reply can later bind;
* :func:`resurface_question_backlog` — the RECURRING nag's DIGEST half
    (directive #36). The two drains above are single-shot per question (each keys
    its idempotency on the row's ``stable_notify_ref``), so a question the owner
    never answers is never raised again. This one re-raises the backlog as ONE
    count per :data:`RESURFACE_INTERVAL_HOURS` bucket in the same DM thread;
* :func:`reask_escalated_questions` — the RECURRING nag's ANSWERABLE half. A
    digest is a place the owner cannot answer from (a reply under it carries the
    digest's thread ts, which joins no question), so the per-question bump is
    posted INTO each question's own mirror thread under the SAME interval bucket.
    It records no row and re-records nothing: ``DeferredQuestion.record`` returns
    the existing pending row for a marker, so a re-ask built on it posts nothing
    at all.

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
from teatree.core.notify import NotifyKind, notify_user, notify_user_outcome
from teatree.core.notify_types import NotifyOptions, NotifyReason

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

#: How many questions one re-ask bucket bumps in their own Slack threads. The
#: digest can name a hundred rows in one message and none of them is answerable
#: there; five bumps are five threads the owner can actually reply into, which is
#: the number that decides how fast the backlog can shrink.
_REASK_BATCH = 5

#: Prefix of the ``BotPing`` idempotency key :func:`reask_escalated_questions` writes.
_REASK_KEY_PREFIX = "reask:"


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


def _reask_text(row: DeferredQuestion, *, now: dt.datetime) -> str:
    """The short bump posted into *row*'s OWN Slack thread.

    The question itself is the thread root three messages up, so re-printing it
    would only push it further away. What the bump adds is the age — the age
    backstop's stamp is otherwise invisible, which makes "escalated" read exactly
    like "ignored" — and the reminder that a reply HERE is what binds.
    """
    waited = (now - row.created_at).days
    escalations = f", escalated {row.escalation_count}x" if row.escalation_count else ""
    return (
        f"*Still waiting on this one* — unanswered {waited}d{escalations}.\n"
        f"_Reply in this thread and it lands on question #{row.pk}; nothing else needs typing._"
    )


def format_backlog_digest(rows: Sequence[DeferredQuestion], *, now: dt.datetime | None = None) -> str:
    """The single recurring nag message covering *rows* — a COUNT, never a list.

    The per-question detail moved into each question's OWN Slack thread
    (:func:`reask_escalated_questions`), which is the only surface a reply to it
    can bind on: a reply under the digest carries the DIGEST's thread ts, so the
    exact ``thread_ts`` → mirror-ts join in :mod:`teatree.loop.question_binding`
    matches nothing, and with a backlog deeper than one the sole-live-question rung
    refuses to guess. Ten question lines in a 147-deep digest were therefore ten
    questions asked where no answer could land.

    What survives is what a count can carry honestly — how many are open, how many
    the age backstop has stamped, and how long the oldest has waited — plus the
    ``#<id> <your answer>`` form, which binds from anywhere in the DM.
    """
    stamped_at = now or timezone.now()
    escalated = sum(1 for row in rows if row.escalated_at is not None)
    oldest = max(((stamped_at - row.created_at).days for row in rows), default=0)
    header = f"*{len(rows)} open question{'s' if len(rows) != 1 else ''} need decisions, oldest is {oldest}d.*"
    if escalated:
        header += f" *{escalated} past the age ceiling.*"
    return "\n".join(
        [
            header,
            f"I'm bumping the {_REASK_BATCH} most urgent in their own threads — reply there to answer one.",
            "Anywhere else, address it as `#<id> <your answer>`.",
        ]
    )


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


def _answerable_options(row: DeferredQuestion, *, backend: "MessagingBackend | None", user_id: str) -> NotifyOptions:
    """Where a post about *row* must land for the owner's reply to bind back to it.

    A row that already carries a mirror OWNS a Slack thread, so the post goes INTO
    it: Slack stamps every reply in a thread with the ROOT's ts, so a reply under
    this post carries ``row.slack_ts`` and rung (b) of
    :mod:`teatree.loop.question_binding` joins it exactly. A row with no mirror yet
    has no thread to ride, so the post goes at the DM ROOT and its own ts becomes
    the row's mirror (:func:`_stamp_mirror_from`) — the same at-root rule, for the
    same reason.

    The one shape that is never right is the default: nesting the post under
    whatever DM thread the owner happens to be in stamps replies with THAT root, so
    the join misses and — at any backlog above one — the sole-question rung refuses
    too. The answer is acknowledged and dropped.
    """
    return NotifyOptions(
        backend=backend,
        user_id=user_id or None,
        thread_ts=row.slack_ts,
        as_thread_root=not row.slack_ts,
    )


def _stamp_mirror_from(row: DeferredQuestion, key: str) -> bool:
    """Read the delivered ``BotPing`` coordinates back onto *row*; ``True`` on the stamp.

    Verify-by-re-read: only a ``SENT`` ping with a real ``posted_ts`` may become a
    binding identity, and :meth:`DeferredQuestion.mark_mirrored` is a single-use CAS
    so a concurrent drain cannot re-stamp.
    """
    ping = BotPing.objects.filter(idempotency_key=key, status=BotPing.Status.SENT).first()
    return bool(ping and ping.posted_ts and row.mark_mirrored(channel=ping.channel_ref, slack_ts=ping.posted_ts))


def reask_escalated_questions(
    *,
    user_id: str = "",
    overlay: str = "",
    backend: "MessagingBackend | None" = None,
    now: dt.datetime | None = None,
) -> tuple[int, int]:
    """Bump the oldest escalated questions IN THEIR OWN THREADS; return ``(bumped, candidates)``.

    The per-question half of the recurring nag, and the half whose answers can
    actually land. It writes NO row and touches no ``slack_ts``: re-asking by
    re-recording is a no-op, because :meth:`DeferredQuestion.record` returns the
    EXISTING pending row for a dedupe marker — a pending row IS the mute — and
    dismissing-and-recreating would break that mute and cut the single-use audit
    chain. So the bump rides the row and the mirror thread it already has, and the
    only new state is one ``BotPing`` under
    ``reask:<stable_notify_ref>:<bucket>``. The bucket is the same
    :data:`RESURFACE_INTERVAL_HOURS` window the digest uses, so every tick inside
    one bucket collapses onto the delivered ping and only a new bucket bumps again.

    The count is of NEW bumps: a key the ledger has already delivered returns a sent
    outcome too, so counting that would report a fresh nag on every tick of the bucket.

    Escalated rows come first and, within each half, the oldest — the rows the age
    backstop has already stamped as sat-past-the-ceiling. Only MIRRORED rows are
    candidates: an un-mirrored row has no thread to bump into, and it belongs to
    :func:`drain_unmirrored_deferred_questions`, which posts its FIRST copy at root
    and stamps the mirror this function then rides.
    """
    rows = [
        row for row in DeferredQuestion.pending() if row.audience != DeferredQuestion.Audience.INTERNAL and row.slack_ts
    ]
    if not rows:
        return 0, 0

    stamped_at = now or timezone.now()
    bucket = int(stamped_at.timestamp()) // (RESURFACE_INTERVAL_HOURS * 3600)
    urgent = sorted(rows, key=lambda row: (row.escalated_at is None, row.created_at))[:_REASK_BATCH]

    previous_overlay = _scoped_overlay_env(overlay)
    bumped = 0
    try:
        for row in urgent:
            outcome = notify_user_outcome(
                _reask_text(row, now=stamped_at),
                kind=NotifyKind.QUESTION,
                idempotency_key=f"{_REASK_KEY_PREFIX}{row.stable_notify_ref}:{bucket}",
                audience=NotifyAudience.OWNER_QUESTION,
                options=_answerable_options(row, backend=backend, user_id=user_id),
            )
            # NEW bumps only. ``sent`` is also true for a key the ledger already
            # delivered, so counting it would report a fresh nag on every tick inside
            # the bucket — the count is what says whether the owner was disturbed.
            if outcome.sent and outcome.reason is not NotifyReason.ALREADY_SENT:
                bumped += 1
    finally:
        _restore_overlay_env(overlay, previous_overlay)
    return bumped, len(rows)


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


def drain_deferred_questions(
    *, user_id: str = "", overlay: str = "", backend: "MessagingBackend | None" = None
) -> tuple[int, int]:
    """Re-post the pending :class:`DeferredQuestion` backlog to the user's Slack DM.

    The single canonical away→present drain. Both the manual
    ``t3 teatree questions resurface`` command and the automatic
    ``write_override(MODE_PRESENT)`` away→present transition call this — one code
    path, no duplicated egress logic.

    Each post lands where a reply to it BINDS — into the question's own mirror
    thread when it has one, else at the DM root with the posted ts stamped as the
    mirror (:func:`_answerable_options`). Nested under the owner's active DM thread
    — this drain's previous shape, with no ``mark_mirrored`` at all — the resurfaced
    copy carried no bindable identity in either direction: a reply to it matched no
    question by thread, and at a backlog above one the sole-question rung refused,
    so the answer was acked and dropped. A question re-asked that way is worse than
    one left alone.

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
            key = f"{_RESURFACE_KEY_PREFIX}{row.stable_notify_ref}"
            # Posted where a reply BINDS — into the row's own mirror thread, or at
            # root and stamped as the mirror when it has none. A resurfaced question
            # the owner cannot answer is worse than one not resurfaced at all: it
            # spends the owner's attention and returns nothing.
            if not notify_user_outcome(
                _resurface_text(row),
                kind=NotifyKind.QUESTION,
                idempotency_key=key,
                audience=NotifyAudience.OWNER_QUESTION,
                options=_answerable_options(row, backend=backend, user_id=user_id),
            ).sent:
                continue
            delivered += 1
            if not row.slack_ts:
                _stamp_mirror_from(row, key)
    finally:
        _restore_overlay_env(overlay, previous_overlay)

    # The denominator is the BACKLOG, never the capped slice: `3/3` read as "all pending
    # delivered" while 69 were outstanding, which is how a permanently-stalled queue
    # looked healthy (#4064).
    return delivered, len(owner_rows)


def _rows_to_mirror(only_ref: str) -> list[DeferredQuestion]:
    pending = DeferredQuestion.unmirrored_pending()
    if not only_ref:
        return list(pending)[:_MAX_MIRRORS_PER_TICK]
    # Selection is oldest-first and the cap bounds a BURST, so a freshly-recorded
    # row queues behind the whole backlog. One targeted row is not a burst.
    return [row for row in pending if row.stable_notify_ref == only_ref][:1]


def drain_unmirrored_deferred_questions(
    *, user_id: str = "", overlay: str = "", backend: "MessagingBackend | None" = None, only_ref: str = ""
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
    owner is away — the lanes keep recording rows and nobody answers them — so
    draining it unbounded turns the moment they come back into one DM per
    accumulated question, seconds apart — 52 in one burst is what this fix was
    written for.
    The owner reads that as spam and mutes the channel, which loses the next
    genuine question too. The remainder is not dropped: `unmirrored_pending()`
    is re-read every tick, so the backlog drains steadily instead of at once,
    and a question that has waited hours can wait one more cadence.

    *only_ref* narrows the batch to the single row with that
    :attr:`~teatree.core.models.deferred_question.DeferredQuestion.stable_notify_ref`
    and bypasses the cap — the capture-time kick the loop-driven ``AskUserQuestion``
    deny arm fires so a headless blocker is not queued behind the backlog.
    """
    rows = _rows_to_mirror(only_ref)
    if not rows:
        return 0, 0

    previous_overlay = _scoped_overlay_env(overlay)
    mirrored = 0
    try:
        for row in rows:
            key = f"mirror-deferred-question:{row.stable_notify_ref}"
            # AT ROOT, never nested — ``_answerable_options`` gives an un-mirrored
            # row exactly that. This ``posted_ts`` becomes the row's ``slack_ts``
            # two lines down, the identity a Slack reply's ``thread_ts`` is joined
            # against by ``teatree.loop.question_binding``. Slack stamps a thread
            # reply with the ROOT's ts, so a mirror nested under the owner's active
            # DM thread is a ts no reply can ever carry: the join misses every time
            # and the answer falls through unbound.
            if not notify_user_outcome(
                _resurface_text(row),
                kind=NotifyKind.QUESTION,
                idempotency_key=key,
                audience=NotifyAudience.OWNER_QUESTION,
                options=_answerable_options(row, backend=backend, user_id=user_id),
            ).sent:
                continue
            if _stamp_mirror_from(row, key):
                mirrored += 1
    finally:
        _restore_overlay_env(overlay, previous_overlay)

    return mirrored, len(rows)
