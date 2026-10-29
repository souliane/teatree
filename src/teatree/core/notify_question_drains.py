"""Tick-level DeferredQuestion → Slack drains over :func:`notify_user`.

The cross-tick re-delivery drains for the durable question backlog, split out
of ``teatree.core.notify`` (at its module-health LOC cap). Both post pending
:class:`DeferredQuestion` rows to the user's DM through the canonical
:func:`teatree.core.notify.notify_user` egress:

* :func:`drain_deferred_questions` — the away→present resurface of the whole
    pending backlog (manual ``questions resurface`` + the auto away→present
    transition);
* :func:`drain_unmirrored_deferred_questions` — the headless ask-loop poster:
    posts rows with no Slack mirror yet under their subject ticket when known,
    otherwise at the DM root, and stamps the card's own mirror coordinates;
* :func:`reask_escalated_questions` — the recurring per-question bump, posted
    in its subject ticket thread when known, otherwise its mirror thread,
    once per escalation generation.
    It records no row and re-records nothing: ``DeferredQuestion.record`` returns
    the existing pending row for a marker, so a re-ask built on it posts nothing
    at all.

The INFO-redelivery peer (``drain_undelivered_notifies``) stays in
``teatree.core.notify`` — a different durability concern (no-backend at post
time vs. no-mirror / away at ask time).
"""

import datetime as dt
import logging
import os
import threading
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from functools import reduce
from operator import or_
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from teatree.core.loop_lease_liveness import namespace_is_proven
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.reask_cadence import REASK_KEY_PREFIX, bump_due, first_posted_at, owner_quiet_at, reask_key
from teatree.core.models import BotPing, DeferredQuestion, LoopLease, ThreadBinding, TicketDmPost, TicketDmThread
from teatree.core.notify import NotifyKind, notify_user_outcome, resolve_owner_dm_backend
from teatree.core.notify_types import NotifyOptions, NotifyOutcome, NotifyReason
from teatree.core.owner_dm.question_ticket import single_question_ticket_id
from teatree.core.owner_dm.ticket_dm_delivery import root_is_deleted
from teatree.core.owner_question_message import (
    bump_text,
    render_blocks,
    render_text,
    replace_root,
    shown_problems_for,
    withheld_message,
)
from teatree.core.question_heal import live_owner_questions, withdraw_healed, withhold
from teatree.loop.preset_resolution import owner_zone

#: Prefix of the ``BotPing`` idempotency key :func:`drain_deferred_questions` writes.
#: The selection reads this ledger back so a settled head never hides a pending row.
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

_MIRROR_KEY_PREFIX = "mirror-deferred-question:"
_STRANDED_SEND_WINDOW = dt.timedelta(hours=1)

_QUESTION_PING_KEY_PREFIXES = (_RESURFACE_KEY_PREFIX, _MIRROR_KEY_PREFIX, REASK_KEY_PREFIX)

#: One sender across every process: a live holder keeps it for its whole send, a dead one frees it.
_QUESTION_PING_SLOT = "work:owner-question-ping"
_QUESTION_PING_SESSION_PREFIX = "question-ping:"
#: ``starttime`` is field 22 of ``/proc/<pid>/stat``; the fields after the ``)``-closed comm start at field 3.
_PROCFS_STARTTIME_INDEX = 19
#: How long a sender whose liveness this venue cannot verify keeps the slot.
_QUESTION_PING_SLOT_TTL_SECONDS = 120
#: Threads of one process share its pid, which the slot's liveness check cannot tell apart.
_QUESTION_PING_THREAD_GUARD = threading.Lock()
#: The measured 11.04s send fits this wait inside the shared 60s scan deadline.
_QUESTION_PING_THREAD_WAIT_SECONDS = 15
_QUESTION_DRAIN_BUDGET_SECONDS = 45
_QUESTION_SEND_RESERVE_SECONDS = 12


def _deadline_for_pass(deadline: float | None) -> float:
    return deadline if deadline is not None else time.monotonic() + _QUESTION_DRAIN_BUDGET_SECONDS


def _send_time_available(deadline: float) -> bool:
    return time.monotonic() + _QUESTION_SEND_RESERVE_SECONDS <= deadline


def _question_ping_keys() -> Q:
    return reduce(or_, (Q(idempotency_key__startswith=prefix) for prefix in _QUESTION_PING_KEY_PREFIXES))


def owner_quiet_now() -> bool:
    """Whether it is 22:00-08:00 in the owner's zone — when no question DM is sent."""
    return owner_quiet_at(timezone.now().astimezone(owner_zone()).time())


@contextmanager
def _question_ping_slot(*, deadline: float | None = None) -> Generator[bool]:
    if owner_quiet_now():
        logger.info("Owner question pings are held until 08:00 (quiet hours)")
        yield False
        return
    end = _deadline_for_pass(deadline)
    if not _send_time_available(end):
        yield False
        return
    wait = min(_QUESTION_PING_THREAD_WAIT_SECONDS, max(0.0, end - time.monotonic() - _QUESTION_SEND_RESERVE_SECONDS))
    if not _QUESTION_PING_THREAD_GUARD.acquire(timeout=wait):
        logger.info(
            "Another drain in this process still holds the owner question-ping slot after %ss; "
            "this ping waits for the next pass",
            wait,
        )
        yield False
        return
    try:
        with _held_question_ping_slot() as held:
            yield held and _send_time_available(end)
    finally:
        _QUESTION_PING_THREAD_GUARD.release()


def _process_start_token(pid: int) -> str:
    """When *pid* started, from procfs, so a pid reused by a later process reads differently; ``""`` if unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    fields = stat.rpartition(")")[2].split()
    return fields[_PROCFS_STARTTIME_INDEX] if len(fields) > _PROCFS_STARTTIME_INDEX else ""


def _pid_names_a_later_process(session: str) -> bool:
    pid_text, _, rest = session.removeprefix(_QUESTION_PING_SESSION_PREFIX).partition(":")
    recorded_start = rest.partition(":")[0]
    current_start = _process_start_token(int(pid_text)) if pid_text.isdigit() and recorded_start else ""
    return bool(current_start) and current_start != recorded_start


def _release_a_stranded_holder() -> None:
    """Free the slot from a sender that cannot still be sending, whether its pid was reused or cannot be verified."""
    holder = (
        LoopLease.objects.filter(name=_QUESTION_PING_SLOT)
        .values("session_id", "owner_pid_namespace", "acquired_at", "lease_expires_at")
        .first()
    )
    if holder is None or not holder["session_id"].startswith(_QUESTION_PING_SESSION_PREFIX):
        return
    session = holder["session_id"]
    # Another container's pid names an unrelated process there, so only a proven namespace may act on the pid.
    if namespace_is_proven(holder["owner_pid_namespace"]):
        if _pid_names_a_later_process(session):
            LoopLease.objects.release_ownership(_QUESTION_PING_SLOT, session_id=session)
        return
    now = timezone.now()
    if holder["lease_expires_at"] is None or holder["lease_expires_at"] >= now:
        return
    with transaction.atomic():
        # A live send can outlast its claim and must keep the serialized slot.
        if BotPing.objects.filter(
            _question_ping_keys(), status=BotPing.Status.SENDING, posted_at__gt=now - _STRANDED_SEND_WINDOW
        ).exists():
            return
        LoopLease.objects.filter(
            name=_QUESTION_PING_SLOT, session_id=session, acquired_at=holder["acquired_at"]
        ).update(session_id="", owner_pid=None, owner_pid_namespace="", acquired_at=None, lease_expires_at=None)


@contextmanager
def _held_question_ping_slot() -> Generator[bool]:
    pid = os.getpid()
    session = f"{_QUESTION_PING_SESSION_PREFIX}{pid}:{_process_start_token(pid)}:{threading.get_ident()}"
    _release_a_stranded_holder()
    won, _holder = LoopLease.objects.claim_ownership(
        _QUESTION_PING_SLOT, session_id=session, owner_pid=pid, ttl_seconds=_QUESTION_PING_SLOT_TTL_SECONDS
    )
    if not won:
        logger.info("Another live sender holds the owner question-ping slot; this ping waits for the next pass")
        yield False
        return
    try:
        yield True
    finally:
        LoopLease.objects.release_ownership(_QUESTION_PING_SLOT, session_id=session)


def _checked(rows: Iterable[DeferredQuestion], *, backend: "MessagingBackend | None") -> list[DeferredQuestion]:
    """The rows that pass the owner-message checks; the others are withheld, never posted, bumped or counted."""
    sendable: list[DeferredQuestion] = []
    for row in rows:
        if not (problems := shown_problems_for(row)):
            sendable.append(row)
        elif withhold(row, problems) and row.slack_ts:
            _replace_root(row, backend)
    return sendable


def _replace_root(row: DeferredQuestion, backend: "MessagingBackend | None") -> None:
    target = backend or resolve_owner_dm_backend()[0]
    if target is None:
        return
    replace_root(row, withheld_message(), target)


def _post(
    row: DeferredQuestion, key: str, *, backend: "MessagingBackend | None", user_id: str, now: dt.datetime
) -> NotifyOutcome:
    """Send a card or chase through its subject ticket when uniquely known."""
    mirrored = bool(row.slack_ts)
    ticket_id = single_question_ticket_id(row)
    old_root = (
        TicketDmThread.objects.filter(ticket_id=ticket_id, channel=row.slack_channel, root_ts=row.slack_ts).exists()
        if mirrored and ticket_id is not None
        else False
    )
    target = (backend or resolve_owner_dm_backend()[0]) if old_root else None
    deleted_root = bool(old_root and target and root_is_deleted(target, row.slack_channel, row.slack_ts))
    outcome = notify_user_outcome(
        bump_text(row, now=now) if mirrored and not deleted_root else render_text(row),
        kind=NotifyKind.QUESTION,
        idempotency_key=key,
        audience=NotifyAudience.OWNER_QUESTION,
        options=NotifyOptions(
            backend=backend,
            user_id=user_id or None,
            thread_ts="" if ticket_id is not None else row.slack_ts,
            fallback_thread_ts="" if deleted_root else row.slack_ts,
            ticket_id=ticket_id,
            question_id=row.pk if ticket_id is not None else None,
            linkify=False,
            blocks=None if mirrored and not deleted_root else render_blocks(row),
        ),
    )
    if deleted_root and outcome.sent:
        ping = BotPing.objects.filter(idempotency_key=key, status=BotPing.Status.SENT).first()
        if (
            ping
            and ping.posted_ts
            and TicketDmPost.objects.filter(channel=ping.channel_ref, slack_ts=ping.posted_ts, question=row).exists()
        ):
            changed = DeferredQuestion.objects.filter(
                pk=row.pk, slack_ts=row.slack_ts, answered_at__isnull=True, dismissed_at__isnull=True
            ).update(slack_ts=ping.posted_ts, slack_channel=ping.channel_ref)
            if changed:
                row.slack_ts = ping.posted_ts
                row.slack_channel = ping.channel_ref
    return outcome


def _stamp_mirror_from(row: DeferredQuestion, key: str) -> bool:
    """Read the delivered ``BotPing`` coordinates back onto *row*; ``True`` on the stamp.

    Verify-by-re-read: only a ``SENT`` ping with a real ``posted_ts`` may become a
    binding identity, and :meth:`DeferredQuestion.mark_mirrored` is a single-use CAS
    so a concurrent drain cannot re-stamp.
    """
    ping = BotPing.objects.filter(idempotency_key=key, status=BotPing.Status.SENT).first()
    stamped = bool(ping and ping.posted_ts and row.mark_mirrored(channel=ping.channel_ref, slack_ts=ping.posted_ts))
    nested = TicketDmPost.objects.filter(channel=row.slack_channel, slack_ts=row.slack_ts, question=row).exists()
    if stamped and not nested and (parked_task := row.parked_task) is not None:
        try:
            ThreadBinding.bind(ticket=parked_task.ticket, channel=row.slack_channel, thread_ts=row.slack_ts)
        except ValueError:
            logger.exception("Question mirror thread binding conflicts with a different ticket")
    return stamped


def reask_escalated_questions(
    *,
    user_id: str = "",
    overlay: str = "",
    backend: "MessagingBackend | None" = None,
    now: dt.datetime | None = None,
    deadline: float | None = None,
) -> tuple[int, int]:
    """Bump the oldest escalated questions IN THEIR OWN THREADS; return ``(bumped, candidates)``.

    The per-question half of the recurring nag, and the half whose answers can
    actually land. It writes NO row and touches no ``slack_ts``: re-asking by
    re-recording is a no-op, because :meth:`DeferredQuestion.record` returns the
    EXISTING pending row for a dedupe marker — a pending row IS the mute — and
    dismissing-and-recreating would break that mute and cut the single-use audit
    chain. So the bump rides the row and its ticket or mirror thread, and the
    only new state is one ``BotPing`` under ``reask:<stable_notify_ref>:<gap>``, where
    ``gap`` is the widening bump index :func:`~teatree.core.modelkit.reask_cadence.bump_due`
    derives from the row's age, so every tick between two bumps collapses onto the delivered ping.

    The count is of NEW bumps: a key the ledger has already delivered returns a sent
    outcome too, so counting that would report a fresh nag on every tick between bumps.

    Escalated rows come first and, within each half, the oldest — the rows the age
    backstop has already stamped as sat-past-the-ceiling. Only MIRRORED rows are
    candidates: an un-mirrored row has no thread to bump into, and it belongs to
    :func:`drain_unmirrored_deferred_questions`, which posts its first copy and
    stamps the card timestamp this function then reuses.

    A row already bumped at its current gap is dropped before the send walk.
    """
    end = _deadline_for_pass(deadline)
    rows = [row for row in _checked(live_owner_questions(), backend=backend) if row.slack_ts]
    if not rows:
        return 0, 0

    stamped_at = now or timezone.now()
    due = [
        (row, gap) for row in rows if (gap := bump_due(first_posted_at(row.slack_ts, row.created_at), now=stamped_at))
    ]
    delivered = BotPing.delivered_keys(reask_key(row.stable_notify_ref, gap) for row, gap in due)
    urgent = sorted(
        ((row, gap) for row, gap in due if reask_key(row.stable_notify_ref, gap) not in delivered),
        key=lambda pair: (pair[0].escalated_at is None, pair[0].created_at),
    )

    previous_overlay = _scoped_overlay_env(overlay)
    bumped = 0
    try:
        for row, gap in urgent:
            with _question_ping_slot(deadline=end) as open_slot:
                if not open_slot:
                    break
                outcome = _post(
                    row, reask_key(row.stable_notify_ref, gap), backend=backend, user_id=user_id, now=stamped_at
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
    *, user_id: str = "", overlay: str = "", backend: "MessagingBackend | None" = None, deadline: float | None = None
) -> tuple[int, int]:
    """Re-post the pending :class:`DeferredQuestion` backlog to the user's Slack DM.

    The single canonical away→present drain. Both the manual
    ``t3 teatree questions resurface`` command and the automatic
    ``write_override(MODE_PRESENT)`` away→present transition call this — one code
    path, no duplicated egress logic.

    A uniquely identified subject routes to its ticket thread. Other cards use
    the existing mirror thread or post at the DM root, and every card retains
    its own message timestamp for reply and button binding.

    Idempotent per question (the ``BotPing`` ledger dedupes the per-question
    ``resurface-deferred-question:<stable-ref>`` key — the row's
    :attr:`~teatree.core.models.deferred_question.DeferredQuestion.stable_notify_ref`,
    never its local pk), so re-running on a later tick or after a manual
    ``resurface`` never double-posts. The ledger is read before the walk so
    settled rows cannot hide pending ones. Each pass stops before its deadline.
    Fails open: a delivery
    failure for one question is recorded on its ``BotPing`` row by
    :func:`notify_user` and never aborts the drain or raises. Returns
    ``(delivered, total)``.
    """
    # DM only owner-audience rows; INTERNAL escalations (repair-loop / dispatch
    # health the box raised about itself) stay logged/statusline-only.
    end = _deadline_for_pass(deadline)
    owner_rows = _checked(live_owner_questions(), backend=backend)
    pending_total = len(owner_rows)
    settled = _refs_the_send_will_not_redeliver()
    rows = [r for r in owner_rows if r.stable_notify_ref not in settled]
    if not rows:
        # Nothing NEW to send. The backlog total is still reported so a caller cannot read
        # "0 delivered" as "queue empty" — the distinction this drain previously erased.
        return 0, pending_total

    previous_overlay = _scoped_overlay_env(overlay)
    delivered = 0
    try:
        for row in rows:
            key = f"{_RESURFACE_KEY_PREFIX}{row.stable_notify_ref}"
            # A resurfaced question the owner cannot answer spends attention for nothing.
            with _question_ping_slot(deadline=end) as open_slot:
                if not open_slot:
                    break
                sent = _post(row, key, backend=backend, user_id=user_id, now=timezone.now()).sent
            if not sent:
                continue
            delivered += 1
            if not row.slack_ts:
                _stamp_mirror_from(row, key)
    finally:
        _restore_overlay_env(overlay, previous_overlay)

    return delivered, pending_total


def drain_unmirrored_deferred_questions(
    *, user_id: str = "", overlay: str = "", backend: "MessagingBackend | None" = None, deadline: float | None = None
) -> tuple[int, int]:
    """Post the un-mirrored :class:`DeferredQuestion` backlog and stamp its mirror.

    The tick-level outbound poster for the headless ask-loop (peer of
    ``drain_undelivered_notifies``): the SDK lane, and any repair-halt row
    recorded before halts became health signals, hold a pending question with no
    ``slack_ts`` and nobody posts it. This drain posts each via
    :func:`notify_user` (idempotent under the
    ``mirror-deferred-question:<stable-ref>`` key — the row's
    :attr:`~teatree.core.models.deferred_question.DeferredQuestion.stable_notify_ref`,
    never its local pk) and, on a confirmed send, reads the delivered ``BotPing`` coordinates
    back and stamps ``slack_ts``/``slack_channel`` so the reply scanner can later
    bind a reply (verify-by-re-read). A row that does not deliver (no backend
    resolved in this context) is left un-mirrored — the durable row IS the
    fallback — and retried next tick. Returns ``(mirrored, total)``.

    The pass stops before its deadline; pending rows remain for the next pass.
    """
    end = _deadline_for_pass(deadline)
    rows = _checked(withdraw_healed(DeferredQuestion.unmirrored_pending()), backend=backend)
    if not rows:
        return 0, 0

    previous_overlay = _scoped_overlay_env(overlay)
    mirrored = 0
    try:
        for row in rows:
            key = f"{_MIRROR_KEY_PREFIX}{row.stable_notify_ref}"
            with _question_ping_slot(deadline=end) as open_slot:
                if not open_slot:
                    break
                sent = _post(row, key, backend=backend, user_id=user_id, now=timezone.now()).sent
            if not sent:
                continue
            if _stamp_mirror_from(row, key):
                mirrored += 1
    finally:
        _restore_overlay_env(overlay, previous_overlay)

    return mirrored, len(rows)
