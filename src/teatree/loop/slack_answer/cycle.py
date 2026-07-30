"""One reactive Slack-answer cycle (#1014).

The reactive, token-cheap complement to the inbound drain: where
``slack_dm_inbound`` only records user DMs and the prompt-drain surfaces
them in-band, this cycle *answers* them out-of-band — event-driven off the
inbound-event wake (~1s), with a 5m fallback timer — so a quick ack / status
question gets a reply in seconds, not at the next slower per-loop tick, and at
near-zero token cost.

It is **complementary to the drain, not a double-answer**: ``consume()``
stamps ``consumed_at`` (prompt-drain), this cycle stamps
``loop_replied_at`` (loop reply posted, #1075 / Option B). It
deliberately does NOT touch ``answered_at`` — that column is #1069's
strict "the agent personally replied" turn-end gate, kept fully
decoupled from this loop's work-queue so a token-cheap loop reply never
silently satisfies the #1063 Stop-hook gate. The columns are orthogonal
single-use CAS transitions, so a row can be drained, loop-replied, and
agent-answered independently with no race and no double reply.

Per unit, oldest-first, bounded to :data:`_BATCH` per cycle. First a
no-LLM :eyes: receipt reaction, exactly once (``mark_eyes_reacted`` CAS)
even across cycle re-runs. Then a route via the zero-token classifier:

- ``ACK_ONLY`` → react ✅ / 🙏, ``mark_loop_replied("ack")``, NO thread post.
- ``SIMPLE`` → :func:`build_simple_answer`; post the threaded reply,
    readback-verify, only THEN ``mark_loop_replied("simple")``. A post or
    readback failure leaves the row loop-unreplied for retry.
- ``NEEDS_WORK`` (or Stage B sentinel / budget-closed) → create ONE
    PENDING ``t3:answerer`` Task (the loop's ``claim-next`` spawns
    the bounded sub-agent — no new spawn path), ``mark_loop_replied(
    "delegated")``. No prose ack is posted (#1155): the :eyes: receipt
    fired earlier is the only acknowledgement; a second arrival with
    "On it" prose is pure noise to a Slack-DM-only user.

Per-unit ``try/except`` so one bad unit never blocks the rest. This is a
management-command body — it never loads the fat skill stack.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import PendingChatInjection, Session, Task, Ticket
from teatree.loop.slack_answer.classifier import AnswerRoute, classify
from teatree.loop.slack_answer.simple_answer import NEEDS_WORK_SENTINEL, build_simple_answer
from teatree.loop.slack_answer.thread_readback import bot_reply_present_in_thread, resolve_thread_root

logger = logging.getLogger(__name__)

_BATCH = 10
_EYES_EMOJI = "eyes"
_ACK_EMOJI = "white_check_mark"
# Consecutive messages from the same user on the same channel, with no
# bot reply between them and received within this window, are one logical
# turn (a message + its quick follow-up). Zero-token: pure DB/time logic.
_COALESCE_WINDOW_SECONDS = 90

type MessagingResolver = Callable[[str], MessagingBackend | None]


@dataclass(slots=True)
class _Unit:
    """One logical turn — one or more coalesced ``PendingChatInjection`` rows.

    The answer threads on the FIRST row's ``slack_ts``; every row in the
    unit gets the :eyes: receipt and is stamped ``loop_replied_at``
    together when the unit is replied to. ``text`` is the newline-joined
    message bodies in original (received) order — a single unit for the
    classifier and the answer builder.
    """

    rows: list[PendingChatInjection] = field(default_factory=list)

    @property
    def lead(self) -> PendingChatInjection:
        return self.rows[0]

    @property
    def channel(self) -> str:
        return self.lead.channel

    @property
    def slack_ts(self) -> str:
        return self.lead.slack_ts

    @property
    def overlay(self) -> str:
        return self.lead.overlay

    @property
    def text(self) -> str:
        return "\n".join(r.text for r in self.rows)


def _coalesce(rows: list[PendingChatInjection]) -> list[_Unit]:
    """Group consecutive same-user/channel rows into logical turns.

    A new unit starts when the next row is from a different
    ``(overlay, channel, user_id)`` OR its ``received_at`` is more than
    :data:`_COALESCE_WINDOW_SECONDS` after the previous row's. Rows arrive
    oldest-first (``loop_unreplied()`` ordering). The loop is the only bot
    that replies and it stamps ``loop_replied_at`` on a whole unit at
    once, so every row reaching this function is pre-reply — "no bot message
    between" reduces to the same-user/within-window adjacency test.
    """
    units: list[_Unit] = []
    for row in rows:
        if units and _continues(units[-1].rows[-1], row):
            units[-1].rows.append(row)
        else:
            units.append(_Unit(rows=[row]))
    return units


def _continues(prev: PendingChatInjection, nxt: PendingChatInjection) -> bool:
    """True iff *nxt* is a follow-up of *prev* (same actor, within window)."""
    if (prev.overlay, prev.channel, prev.user_id) != (nxt.overlay, nxt.channel, nxt.user_id):
        return False
    if not nxt.user_id:
        # No user attribution → cannot prove same author; never coalesce.
        return False
    gap = (nxt.received_at - prev.received_at).total_seconds()
    return 0 <= gap <= _COALESCE_WINDOW_SECONDS


@dataclass(slots=True)
class SlackAnswerReport:
    """One cycle's outcome — for the mgmt command's ``--json`` report."""

    processed: int = 0
    eyes_reacted: int = 0
    acked: int = 0
    answered_simple: int = 0
    delegated: int = 0
    errors: int = 0
    skipped_no_backend: int = 0


def _default_resolver(overlay: str) -> MessagingBackend | None:
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415 — deferred: loaded at tick time

    return messaging_from_overlay(overlay or None)


def verify_reply_visible(backend: MessagingBackend, *, channel: str, thread_root: str) -> bool:
    """Confirm the just-posted reply is visible under its thread ROOT (#2061).

    Reads the thread root's replies and confirms a bot reply is present. The
    key is the thread ROOT, not the user-message ts: a reply posted with
    ``thread_ts=<a non-root user-message ts>`` re-parents to the root, so a
    read-back keyed on the user-message ts misses it and would wrongly stamp
    a delivered reply as absent (or, on the dedup side, post a duplicate). An
    absent reply — including the conservative outcome of an empty/raised read
    — means the caller does NOT stamp ``loop_replied_at`` and the row retries
    next cycle (never stamp on an unconfirmed post).
    """
    return bot_reply_present_in_thread(backend, channel=channel, thread_root=thread_root)


def _mark_unit_loop_replied(unit: _Unit, kind: str) -> bool:
    """CAS ``mark_loop_replied`` on the lead; stamp every coalesced row to match.

    The lead's CAS is the single idempotency boundary (the row that wins
    creates the side effect). The follow-up rows are stamped best-effort
    so they drop out of ``loop_unreplied()`` together with the lead — one
    logical turn, one loop reply, no orphaned follow-up re-processed alone.
    Stamps only ``loop_replied_at`` (#1075); never ``answered_at`` so the
    #1063 turn-end gate stays decoupled from this loop.
    """
    if not unit.lead.mark_loop_replied(kind):
        return False
    for follow in unit.rows[1:]:
        follow.mark_loop_replied(kind)
    return True


def _unmark_unit_loop_replied(unit: _Unit) -> None:
    """Release the whole unit's loop-reply claim — the rollback of :func:`_mark_unit_loop_replied`."""
    for row in unit.rows:
        row.unmark_loop_replied()


def _react_eyes_once(backend: MessagingBackend, unit: _Unit) -> bool:
    """No-LLM receipt reaction on every row of the unit, each at most once.

    Claim -> react -> release-on-failure (#1880): each row's CAS
    ``mark_eyes_reacted`` claims the slot BEFORE the reaction so a
    concurrent cycle cannot also react; if ``backend.react`` raises, the
    claim is released so the row is reacted again next cycle instead of
    carrying a receipt for a reaction that never landed. The raise still
    propagates to the per-unit handler in :func:`run_slack_answer_cycle`,
    which logs and moves on — the released row simply retries.
    """
    reacted = False
    for row in unit.rows:
        if row.eyes_reacted_at is not None or not row.mark_eyes_reacted():
            continue
        try:
            backend.react(channel=row.channel, ts=row.slack_ts, emoji=_EYES_EMOJI)
        except Exception:
            row.unmark_eyes_reacted()
            raise
        reacted = True
    return reacted


def _handle_ack(backend: MessagingBackend, unit: _Unit) -> bool:
    """React ✅ on the lead, mark the whole unit answered, NO thread reply.

    Claim -> react -> release-on-failure (#1880): the unit's loop-reply CAS
    claims the slot BEFORE the ✅ reaction (so a concurrent cycle that lost
    the CAS skips), then the reaction lands; if it raises, the whole unit's
    claim is released so the unit re-enters ``loop_unreplied()`` and retries
    next cycle. Mirrors ``react_merge_on_post``'s claim/release pattern.
    """
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.ACK):
        return False
    try:
        backend.react(channel=unit.channel, ts=unit.slack_ts, emoji=_ACK_EMOJI)
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    return True


def _handle_simple(backend: MessagingBackend, unit: _Unit) -> str:
    """SIMPLE path: resolve-root, dedup, post, readback-verify, stamp.

    Returns an outcome tag: ``"simple"`` — answered & whole unit stamped;
    ``"needs_work"`` — Stage B bailed (delegate); ``"retry"`` —
    post/readback failed, unit left unanswered for next cycle.

    The thread ROOT (resolved from the user-message ts via #2061's
    helper) is the single key used for both the pre-post dedup and the
    post-delivery verification. A reply re-parents to the root, so keying
    either read on the user-message ts (which may be a non-root reply)
    would miss the reply — the bug this path fixes (duplicate answer +
    false "undelivered" verdict). The dedup short-circuit makes the post
    idempotent across cooperating answerers that do not share the
    ``mark_loop_replied`` CAS (#2061's cross-agent duplicate incident).
    """
    answer = build_simple_answer(unit.lead)
    if answer is None or answer == NEEDS_WORK_SENTINEL:
        return "needs_work"
    thread_root = resolve_thread_root(backend, channel=unit.channel, ts=unit.slack_ts)
    if bot_reply_present_in_thread(backend, channel=unit.channel, thread_root=thread_root):
        _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.SIMPLE)
        return "simple"
    backend.post_reply(channel=unit.channel, ts=unit.slack_ts, text=answer)
    if not verify_reply_visible(backend, channel=unit.channel, thread_root=thread_root):
        return "retry"
    _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.SIMPLE)
    return "simple"


def _delegate_needs_work(backend: MessagingBackend, unit: _Unit) -> bool:
    """Create ONE PENDING t3:answerer Task for the whole unit.

    The lead's CAS ``mark_loop_replied("delegated")`` is the idempotency
    boundary: the Task is created only by the cycle whose CAS wins, so a
    re-run (or a concurrent cycle) never enqueues a second answerer Task
    for the same logical turn. The loop has no Agent tool — the loop's
    atomic ``t3 loop claim-next`` (now routing ``(author, answering)`` →
    ``t3:answerer``) spawns the bounded sub-agent; no new spawn path. The
    sub-agent receives the FULL coalesced question, not a fragment.

    No instant-ack prose is posted (#1155). The :eyes: receipt reaction
    fired earlier in :func:`_process_unit` is the only acknowledgement —
    the user reads Slack DMs only, so every thread reply is a phone
    notification, and a content-free "On it" message is pure noise on
    top of the :eyes: signal the user already received.
    """
    _ = backend  # transport still resolved per-unit; no post side effect on delegation
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.DELEGATED):
        return False
    ticket = Ticket.objects.create(
        overlay=unit.overlay,
        role=Ticket.Role.AUTHOR,
        extra={
            "slack_answer": {
                "channel": unit.channel,
                "slack_ts": unit.slack_ts,
                "question": unit.text,
                "coalesced_ts": [r.slack_ts for r in unit.rows],
            }
        },
    )
    session = Session.objects.create(ticket=ticket, overlay=unit.overlay, agent_id="answering")
    Task.objects.create(
        ticket=ticket,
        session=session,
        phase="answering",
        execution_target=Task.ExecutionTarget.HEADLESS,
        execution_reason=(f"Answer the user's Slack message at ts={unit.slack_ts}: {unit.text}"),
    )
    return True


def _process_unit(
    backend: MessagingBackend,
    unit: _Unit,
    report: SlackAnswerReport,
) -> None:
    if _react_eyes_once(backend, unit):
        report.eyes_reacted += 1

    route = classify(unit.text)
    if route is AnswerRoute.ACK_ONLY:
        if _handle_ack(backend, unit):
            report.acked += 1
        return
    if route is AnswerRoute.SIMPLE:
        outcome = _handle_simple(backend, unit)
        if outcome == "simple":
            report.answered_simple += 1
            return
        if outcome == "retry":
            return  # leave unanswered, retry next cycle
        # else: Stage B bailed → fall through to delegation
    if _delegate_needs_work(backend, unit):
        report.delegated += 1


def run_slack_answer_cycle(
    *,
    messaging_resolver: MessagingResolver | None = None,
    now: dt.datetime | None = None,
) -> SlackAnswerReport:
    """Run one bounded reactive Slack-answer cycle (DI-able, deterministic).

    *messaging_resolver* maps an overlay name to its
    :class:`MessagingBackend` (defaults to the per-overlay factory);
    tests inject a recording fake. *now* is accepted for signature
    symmetry with ``schedule.run_tier`` (the model CAS uses
    ``timezone.now`` internally).
    """
    del now  # reserved for symmetry; the CAS stamps use timezone.now()
    resolver = messaging_resolver or _default_resolver
    report = SlackAnswerReport()

    rows = list(PendingChatInjection.loop_unreplied()[:_BATCH])
    units = _coalesce(rows)
    for unit in units:
        report.processed += len(unit.rows)
        try:
            backend = resolver(unit.overlay)
            if backend is None:
                report.skipped_no_backend += 1
                continue
            _process_unit(backend, unit, report)
        except Exception as exc:  # noqa: BLE001 — one bad unit never blocks the rest
            report.errors += 1
            logger.warning("Slack-answer unit (lead row %s) failed: %s", unit.lead.pk, exc)
    return report


__all__ = ["SlackAnswerReport", "run_slack_answer_cycle", "verify_reply_visible"]
