"""One reactive Slack-answer cycle (#1014).

Where ``SlackDmRecorder`` only records user DMs, this cycle *answers* them
out-of-band — event-driven off the inbound-event wake (~1s), with a 5m fallback
timer — so a quick ack / status question gets a reply in seconds, not at the next
slower per-loop tick, and at near-zero token cost.

This cycle stamps ``loop_replied_at`` (loop reply posted, #1075 / Option B) and
deliberately does NOT touch ``answered_at`` — #1069's strict "the agent personally
replied" record, so a token-cheap loop reply never reads as one. The columns are
orthogonal single-use CAS transitions, so a row can be loop-replied and
agent-answered independently with no race and no double reply.

Per unit, oldest-first, bounded to :data:`_BATCH` per cycle. First a
no-LLM :eyes: receipt reaction, exactly once (``mark_eyes_reacted`` CAS)
even across cycle re-runs. A bound thread reaches its ticket's live session
first, including a reply to a queued ``DeferredQuestion``. When no live
session accepts it, :func:`~teatree.loop.question_binding.bind_reply` can
resolve the answer before ordinary routing, without a model turn. Otherwise
the unit is READ (:func:`~teatree.loop.inbound_reading.read_inbound` — one cheap
model turn, keyword fallback) and routed on what it means, not on which
keywords it contains:

- **nothing to do** (an FYI, a bare thanks) → react 🙏 NOTED,
    ``mark_loop_replied("ack")``, NO thread post.
- **an answerable question** → :func:`build_simple_answer`; post the
    threaded reply, read-back-verify, only THEN ``mark_loop_replied("simple")``
    and react ✅ DONE. A post or read-back failure leaves the row
    loop-unreplied for retry and stamps nothing.
- **anything implying work** — an instruction, a correction, or a question
    that needs investigation — is ORCHESTRATED: the request is deduplicated
    against the control plane's live lanes
    (:func:`~teatree.loop.slack_answer.orchestration.find_coverage`) and
    either ONE PENDING task is dispatched (the loop's ``claim-next`` spawns
    the bounded sub-agent — no new spawn path) or the covering lane is
    reported in-thread. Either way the unit is reacted 🔧 IN FLIGHT.

Two rules govern what the owner sees, because Slack is the only surface he
reads. **The reaction vocabulary is disjoint** — receipt, in-flight, noted
and done are four different emojis, so "seen" can never be mistaken for
"handled" (:mod:`~teatree.loop.slack_answer.vocabulary`). And **✅ is placed
only behind a verified delivery**: dispatching work is not completing it.
A fresh dispatch posts no prose (#1155 — the reaction carries it), while a
request that is already covered DOES post, because "task 42 has this" is
information the owner does not otherwise have and is what stops him waiting.

Per-unit ``try/except`` so one bad unit never blocks the rest. This is a
management-command body — it never loads the fat skill stack.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import DeferredQuestion, PendingChatInjection, SessionCommand, ThreadBinding
from teatree.core.models.thread_binding import slack_command_id
from teatree.core.ticket_dm_delivery import record_ticket_thread_post
from teatree.core.worktree.survivor_notice import apply_stop_answer
from teatree.loop.inbound_reading import InboundIntent, InboundReader, InboundReading, memoized_reader, read_inbound
from teatree.loop.question_binding import (
    AmbiguousCards,
    BoundAnswer,
    apply_bound_answer,
    bind_reply,
    log_coalesced_replies,
)
from teatree.loop.slack_answer.coalescing import _coalesce, _Unit
from teatree.loop.slack_answer.orchestration import Coverage, WorkOrigin, dispatch_work, find_coverage, work_fingerprint
from teatree.loop.slack_answer.session_routing import (
    _card_result_reply,
    _command_reply,
    _live_receipt,
    _mark_unit_loop_replied,
    _post_live_reply,
    _reject_foreign_bound_unit,
    _uncertain_question_reply,
    _unmark_unit_loop_replied,
)
from teatree.loop.slack_answer.simple_answer import (
    NEEDS_WORK_SENTINEL,
    build_simple_answer,
    is_status_question,
    ticket_status_answer,
)
from teatree.loop.slack_answer.thread_readback import (
    ExpectedReply,
    bot_reply_match_in_thread,
    bot_reply_present_in_thread,
    resolve_thread_root,
)
from teatree.loop.slack_answer.vocabulary import InboundReaction

logger = logging.getLogger(__name__)

_BATCH = 10
type MessagingResolver = Callable[[str], MessagingBackend | None]


@dataclass(slots=True)
class SlackAnswerReport:
    """One cycle's outcome — for the mgmt command's ``--json`` report.

    ``dispatched`` and ``covered`` are the orchestration halves and are
    deliberately separate: they answer "how much work did this cycle create"
    versus "how much did it decline to duplicate", and collapsing them would
    hide a dedupe that stopped working.
    """

    processed: int = 0
    eyes_reacted: int = 0
    acked: int = 0
    answered_simple: int = 0
    dispatched: int = 0
    covered: int = 0
    answered_question: int = 0
    steered: int = 0
    inspected: int = 0
    errors: int = 0
    skipped_no_backend: int = 0


def _default_resolver(overlay: str) -> MessagingBackend | None:
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415 — deferred: loaded at tick time

    return messaging_from_overlay(overlay or None)


def verify_reply_visible(
    backend: MessagingBackend,
    *,
    channel: str,
    thread_root: str,
    after_ts: str | None = None,
    expected_ts: str | ExpectedReply | None = None,
) -> bool:
    """Confirm the just-posted reply is visible under its thread ROOT (#2061).

    Reads the thread root's replies and confirms a bot reply is present. The
    key is the thread ROOT, not the user-message ts: a reply posted with
    ``thread_ts=<a non-root user-message ts>`` re-parents to the root, so a
    read-back keyed on the user-message ts misses it and would wrongly stamp
    a delivered reply as absent (or, on the dedup side, post a duplicate). An
    absent reply — including the conservative outcome of an empty/raised read
    — releases the provisional claim; no confirmed receipt is stamped.
    """
    return bot_reply_present_in_thread(
        backend,
        channel=channel,
        thread_root=thread_root,
        after_ts=after_ts,
        expected_ts=expected_ts,
    )


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
            backend.react(channel=row.channel, ts=row.slack_ts, emoji=InboundReaction.RECEIVED)
        except Exception:
            row.unmark_eyes_reacted()
            raise
        reacted = True
    return reacted


def _handle_noted(backend: MessagingBackend, unit: _Unit) -> bool:
    """React 🙏 NOTED on the lead, mark the whole unit answered, NO thread reply.

    NOTED, not DONE. A "thanks" closes nothing and an FYI asks for nothing, so
    the honest symbol says "read, nothing to do" — the completion emoji here
    would claim the factory finished something it was never asked to start.

    Claim -> react -> release-on-failure (#1880): the unit's loop-reply CAS
    claims the slot BEFORE the reaction (so a concurrent cycle that lost the
    CAS skips), then the reaction lands; if it raises, the whole unit's claim
    is released so the unit re-enters ``loop_unreplied()`` and retries next
    cycle. Mirrors ``react_merge_on_post``'s claim/release pattern.
    """
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.ACK):
        return False
    try:
        backend.react(channel=unit.channel, ts=unit.slack_ts, emoji=InboundReaction.NOTED)
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    return True


def _handle_simple(backend: MessagingBackend, unit: _Unit, binding: ThreadBinding | None = None) -> str:
    """SIMPLE path: resolve-root, dedup, post, readback-verify, stamp.

    Returns an outcome tag: ``"simple"`` — answered & whole unit stamped;
    ``"needs_work"`` — Stage B bailed (delegate); ``"retry"`` —
    post/readback failed (or a rival cycle owns the unit), left for next cycle.

    Claim -> post -> release-on-failure, the same discipline
    :func:`_handle_noted` and :func:`_orchestrate` hold: the unit's CAS is taken
    BEFORE the post, so a concurrent cycle that lost it cannot post a second copy
    of the same answer. It is released on any post/read-back failure, and the
    thread-root dedup below then finds a reply that DID land, so a released unit
    is re-stamped rather than answered twice.

    The thread ROOT (resolved from the user-message ts via #2061's
    helper) is the single key used for both the pre-post dedup and the
    post-delivery verification. A reply re-parents to the root, so keying
    either read on the user-message ts (which may be a non-root reply)
    would miss the reply — the bug this path fixes (duplicate answer +
    false "undelivered" verdict). The dedup short-circuit makes the post
    idempotent across cooperating answerers that do not share the
    ``mark_loop_replied`` CAS (#2061's cross-agent duplicate incident).
    """
    answer = (
        ticket_status_answer(binding.ticket)
        if binding is not None and is_status_question(unit.text)
        else build_simple_answer(unit.lead)
    )
    if answer is None or answer == NEEDS_WORK_SENTINEL:
        return "needs_work"
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.SIMPLE):
        return "retry"
    try:
        thread_root = resolve_thread_root(backend, channel=unit.channel, ts=unit.slack_ts)
        floor = unit.rows[-1].slack_ts
        matched_ts = bot_reply_match_in_thread(backend, channel=unit.channel, thread_root=thread_root, after_ts=floor)
        if matched_ts is None:
            post = backend.post_reply(channel=unit.channel, ts=unit.slack_ts, text=answer)
            posted_ts = post.get("ts")
            matched_ts = (
                bot_reply_match_in_thread(
                    backend, channel=unit.channel, thread_root=thread_root, after_ts=floor, expected_ts=posted_ts
                )
                if post.get("ok") is True and isinstance(posted_ts, str)
                else None
            )
            if matched_ts is None:
                _unmark_unit_loop_replied(unit)
                return "retry"
        record_ticket_thread_post(unit.channel, thread_root, matched_ts)
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    for row in unit.rows:
        row.observe_confirmed_loop_reply()
    _react_done(backend, unit)
    return "simple"


def _react_done(backend: MessagingBackend, unit: _Unit) -> None:
    """React ✅ DONE — reachable ONLY once the answer is verified visible.

    This is the single call site of the completion emoji in the cycle, which is
    what makes "nothing is marked complete that was not" a structural property
    rather than a convention: every other route reaches for
    :attr:`~teatree.loop.slack_answer.vocabulary.InboundReaction.NOTED` or
    ``IN_FLIGHT``. A failure to place it is logged and swallowed — the answer is
    already delivered, and retrying the whole unit would post it twice.
    """
    try:
        backend.react(channel=unit.channel, ts=unit.slack_ts, emoji=InboundReaction.DONE)
    except Exception as exc:  # noqa: BLE001 — the answer landed; a missing emoji never re-posts it
        logger.warning("Completion react failed for %s/%s: %s", unit.channel, unit.slack_ts, exc)


def _orchestrate(
    backend: MessagingBackend,
    unit: _Unit,
    reading: InboundReading,
    report: SlackAnswerReport,
    binding: ThreadBinding | None = None,
) -> None:
    """Dedupe the request, then either dispatch ONE lane or report the covering one.

    The lead's CAS ``mark_loop_replied("delegated")`` is claimed FIRST and stays
    the idempotency boundary, so a concurrent cycle that lost it does nothing at
    all. Inside that claim the coverage question is asked before anything is
    created — the owner's instruction, and the reason a repeated report of the
    same problem cannot mint a rival lane in a shared checkout.

    EVERY failure path under the claim releases it, because the claim is what
    removes the unit from ``loop_unreplied()``: a coverage lookup that raises, a
    dispatch that dies part-way through its Ticket/Session/Task writes, an
    undeliverable coverage report, or a failed 🔧 reaction would each otherwise
    retire the owner's message with no lane and no reply. Every one is retry-safe —
    the retry finds the lane it just minted
    (:attr:`~teatree.loop.slack_answer.orchestration.CoverageKind.THIS_MESSAGE`),
    so it re-reacts instead of dispatching twice.

    An unbound fresh dispatch posts no prose (#1155); a bound thread gets a
    receipt naming the queued follow-up. A COVERED request names its lane.
    """
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.DELEGATED):
        return
    try:
        _dispatch_or_report(backend, unit, reading, report, binding)
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise


def _dispatch_or_report(
    backend: MessagingBackend,
    unit: _Unit,
    reading: InboundReading,
    report: SlackAnswerReport,
    binding: ThreadBinding | None,
) -> None:
    """The claimed body of :func:`_orchestrate` — dispatch ONE lane, or name the covering one."""
    coalesced = tuple(row.slack_ts for row in unit.rows)
    coverage = find_coverage(
        fingerprint=work_fingerprint(reading, unit.text),
        slack_ts=unit.slack_ts,
        coalesced_ts=coalesced,
        overlay=unit.overlay,
        text=unit.text,
    )
    if coverage is not None and not coverage.is_this_messages_own_lane:
        if _report_coverage(backend, unit, coverage):
            report.covered += 1
        else:
            _unmark_unit_loop_replied(unit)
        return
    if coverage is None:
        dispatch_work(
            reading=reading,
            fingerprint=work_fingerprint(reading, unit.text),
            origin=WorkOrigin(
                overlay=unit.overlay,
                channel=unit.channel,
                slack_ts=unit.slack_ts,
                coalesced_ts=coalesced,
                text=unit.text,
                thread_root=unit.lead.thread_ts,
            ),
        )
    reaction = backend.react(channel=unit.channel, ts=unit.slack_ts, emoji=InboundReaction.IN_FLIGHT)
    if reaction.get("ok") is not True and reaction.get("error") != "already_reacted":
        msg = "in-flight reaction was not confirmed by Slack"
        raise RuntimeError(msg)
    if binding is not None and coverage is None:
        command = SessionCommand.objects.get(command_id=slack_command_id(unit.overlay, unit.channel, unit.slack_ts))
        if not _post_live_reply(backend, unit, _command_reply(command)):
            msg = "queued follow-up receipt was not confirmed by Slack"
            raise RuntimeError(msg)
    for row in unit.rows:
        row.observe_confirmed_loop_reply()
    report.dispatched += 1


def _report_coverage(backend: MessagingBackend, unit: _Unit, coverage: Coverage) -> bool:
    """Tell the owner which lane already has this; ``True`` once it is delivered.

    Same post/read-back discipline as the answer path, keyed on the thread ROOT
    (#2061), so the message is confirmed visible before the claim is kept. The
    unit is then reacted 🔧 IN FLIGHT — the covering lane is running, which is
    not the same as finished.
    """
    thread_root = resolve_thread_root(backend, channel=unit.channel, ts=unit.slack_ts)
    floor = unit.rows[-1].slack_ts
    matched_ts = bot_reply_match_in_thread(backend, channel=unit.channel, thread_root=thread_root, after_ts=floor)
    if matched_ts is None:
        post = backend.post_reply(
            channel=unit.channel,
            ts=unit.slack_ts,
            text=f"Already covered by {coverage.describe()} — not dispatching a second lane.",
        )
        posted_ts = post.get("ts")
        matched_ts = (
            bot_reply_match_in_thread(
                backend, channel=unit.channel, thread_root=thread_root, after_ts=floor, expected_ts=posted_ts
            )
            if post.get("ok") is True and isinstance(posted_ts, str)
            else None
        )
        if matched_ts is None:
            return False
    record_ticket_thread_post(unit.channel, thread_root, matched_ts)
    for row in unit.rows:
        row.observe_confirmed_loop_reply()
    backend.react(channel=unit.channel, ts=unit.slack_ts, emoji=InboundReaction.IN_FLIGHT)
    return True


def _answer_bound_question(backend: MessagingBackend, unit: _Unit, bound: BoundAnswer | None) -> bool:
    """Apply the unit to the queued question it answers; ``True`` when it resolved one.

    The FIRST rung, ahead of the routing read. This cycle wakes ~1s after an
    inbound event while the reply scanner runs on the slower tick, so it reaches
    nearly every owner reply first — and while it knew nothing about
    :class:`DeferredQuestion` it stamped ``loop_replied_at`` on the way past,
    leaving the binder a queue that no longer held the row. Sharing
    :func:`~teatree.loop.question_binding.bind_reply` ends that race by making
    the fast consumer question-aware, rather than by trying to order two
    independent loops.

    Claim -> apply -> ✅, mirroring the scanner: the unit's CAS is the
    idempotency boundary, and a lost apply (a concurrent answer won) releases it
    so nothing carries a receipt for a question it did not resolve.
    """
    if bound is None:
        return False
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.QUESTION_REPLY):
        return False
    if not apply_bound_answer(bound, backend=backend, on_answer=apply_stop_answer):
        _unmark_unit_loop_replied(unit)
        return False
    parked_task = bound.question.parked_task
    if parked_task is not None and bound.question.audience == DeferredQuestion.Audience.OWNER_QUESTION:
        command, _ = SessionCommand.objects.get_or_create(
            command_id=slack_command_id(unit.overlay, unit.channel, unit.slack_ts),
            defaults={
                "ticket_id": parked_task.ticket_id,
                "task_id": parked_task.pk,
                "text": unit.text,
                "outcome": "answered_resume",
            },
        )
        if not _post_live_reply(backend, unit, _command_reply(command)):
            msg = "resume-path receipt was not confirmed by Slack"
            raise RuntimeError(msg)
    for row in unit.rows:
        row.observe_confirmed_loop_reply()
    _react_done(backend, unit)
    return True


def _answer_or_acknowledge_question(
    backend: MessagingBackend, unit: _Unit, report: SlackAnswerReport, bound: BoundAnswer | None
) -> bool:
    if _answer_bound_question(backend, unit, bound):
        report.answered_question += 1
        return True
    if unit.lead.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY:
        return _uncertain_question_reply(backend, unit, report)
    return False


def _process_unit(
    backend: MessagingBackend,
    unit: _Unit,
    report: SlackAnswerReport,
    reader: InboundReader,
) -> None:
    if _react_eyes_once(backend, unit):
        report.eyes_reacted += 1

    binding = (
        ThreadBinding.objects.filter(overlay=unit.overlay, channel=unit.channel, thread_ts=unit.lead.thread_ts).first()
        if unit.lead.thread_ts
        else None
    )
    if _reject_foreign_bound_unit(backend, unit, report, binding):
        return

    if binding is not None and is_status_question(unit.text):
        if _handle_simple(backend, unit, binding) == "simple":
            report.answered_simple += 1
        return

    read = memoized_reader(reader)
    result = bind_reply(unit.lead, reader=read, backend=backend, text=unit.text)
    log_coalesced_replies(unit.rows, result)
    bound = result if isinstance(result, BoundAnswer) else None
    if isinstance(result, AmbiguousCards) and _card_result_reply(backend, unit, report, result):
        return
    if binding is not None and _live_receipt(backend, unit, report, binding, (read, bound)):
        return
    if _card_result_reply(backend, unit, report, result):
        return
    if _answer_or_acknowledge_question(backend, unit, report, bound):
        return

    _route_unbound(backend, unit, report, read, binding)


def _route_unbound(
    backend: MessagingBackend,
    unit: _Unit,
    report: SlackAnswerReport,
    read: InboundReader,
    binding: ThreadBinding | None,
) -> None:
    reading = read(unit.text)
    if reading.needs_nothing:
        if _handle_noted(backend, unit):
            report.acked += 1
        return
    if reading.intent is InboundIntent.QUESTION and reading.answerable:
        outcome = _handle_simple(backend, unit, binding)
        if outcome == "simple":
            report.answered_simple += 1
            return
        if outcome == "retry":
            return  # leave unanswered, retry next cycle
        # else: the cheap answer bailed → the question needs a lane after all
    _orchestrate(backend, unit, reading, report, binding)


def run_slack_answer_cycle(
    *,
    messaging_resolver: MessagingResolver | None = None,
    reader: InboundReader | None = None,
    now: dt.datetime | None = None,
) -> SlackAnswerReport:
    """Run one bounded reactive Slack-answer cycle (DI-able, deterministic).

    *messaging_resolver* maps an overlay name to its
    :class:`MessagingBackend` (defaults to the per-overlay factory);
    tests inject a recording fake. *reader* resolves one message's meaning
    (defaults to :func:`~teatree.loop.inbound_reading.read_inbound`, the
    model turn with its keyword fallback); a test injects a fixed reading so
    the routing is exercised without a model. *now* is accepted for signature
    symmetry with ``schedule.run_tier`` (the model CAS uses
    ``timezone.now`` internally).
    """
    del now  # reserved for symmetry; the CAS stamps use timezone.now()
    resolver = messaging_resolver or _default_resolver
    resolved_reader = reader or read_inbound
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
            _process_unit(backend, unit, report, resolved_reader)
        except Exception as exc:  # noqa: BLE001 — one bad unit never blocks the rest
            report.errors += 1
            logger.warning("Slack-answer unit (lead row %s) failed: %s", unit.lead.pk, exc)
    return report


__all__ = ["SlackAnswerReport", "run_slack_answer_cycle", "verify_reply_visible"]
