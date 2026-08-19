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
even across cycle re-runs. Then, BEFORE any read, the unit is offered to
:func:`~teatree.loop.question_binding.bind_reply`: a reply that answers a
queued ``DeferredQuestion`` resolves it and stops there — an option pick with
no model turn at all, free text on the single reading the routing would have
cost anyway. That rung is the whole reason this cycle may drain the queue at
all: it shares the reply scanner's binder instead of racing it. Otherwise the
unit is READ (:func:`~teatree.loop.inbound_reading.read_inbound` — one cheap
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
from dataclasses import dataclass, field

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import PendingChatInjection
from teatree.loop.inbound_reading import InboundIntent, InboundReader, InboundReading, read_inbound
from teatree.loop.question_binding import apply_bound_answer, bind_reply
from teatree.loop.slack_answer.orchestration import Coverage, WorkOrigin, dispatch_work, find_coverage, work_fingerprint
from teatree.loop.slack_answer.simple_answer import NEEDS_WORK_SENTINEL, build_simple_answer
from teatree.loop.slack_answer.thread_readback import bot_reply_present_in_thread, resolve_thread_root
from teatree.loop.slack_answer.vocabulary import InboundReaction

logger = logging.getLogger(__name__)

_BATCH = 10
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
    ``(overlay, channel, user_id)``, sits in a different Slack thread, OR its
    ``received_at`` is more than :data:`_COALESCE_WINDOW_SECONDS` after the
    previous row's. Rows arrive oldest-first (``loop_unreplied()`` ordering).
    The loop is the only bot that replies and it stamps ``loop_replied_at`` on
    a whole unit at once, so every row reaching this function is pre-reply —
    "no bot message between" reduces to the same-actor/same-thread/within-window
    adjacency test.
    """
    units: list[_Unit] = []
    for row in rows:
        if units and _continues(units[-1].rows[-1], row):
            units[-1].rows.append(row)
        else:
            units.append(_Unit(rows=[row]))
    return units


def _continues(prev: PendingChatInjection, nxt: PendingChatInjection) -> bool:
    """True iff *nxt* is a follow-up of *prev* (same actor, same thread, within window)."""
    if (prev.overlay, prev.channel, prev.user_id) != (nxt.overlay, nxt.channel, nxt.user_id):
        return False
    if prev.thread_ts != nxt.thread_ts:
        # Two threads are two conversations: a unit binds on its LEAD's thread,
        # so coalescing across them feeds one question the other's answer.
        return False
    if not nxt.user_id:
        # No user attribution → cannot prove same author; never coalesce.
        return False
    gap = (nxt.received_at - prev.received_at).total_seconds()
    return 0 <= gap <= _COALESCE_WINDOW_SECONDS


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
        _react_done(backend, unit)
        return "simple"
    backend.post_reply(channel=unit.channel, ts=unit.slack_ts, text=answer)
    if not verify_reply_visible(backend, channel=unit.channel, thread_root=thread_root):
        return "retry"
    _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.SIMPLE)
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


def _orchestrate(backend: MessagingBackend, unit: _Unit, reading: InboundReading, report: SlackAnswerReport) -> None:
    """Dedupe the request, then either dispatch ONE lane or report the covering one.

    The lead's CAS ``mark_loop_replied("delegated")`` is claimed FIRST and stays
    the idempotency boundary, so a concurrent cycle that lost it does nothing at
    all. Inside that claim the coverage question is asked before anything is
    created — the owner's instruction, and the reason a repeated report of the
    same problem cannot mint a rival lane in a shared checkout.

    Two failure paths, both retry-safe. A coverage report that cannot be
    delivered releases the claim, so the unit is re-read next cycle and the
    owner is never left with silence. A dispatch whose 🔧 reaction fails also
    releases the claim — and the retry finds the lane it just minted
    (:attr:`~teatree.loop.slack_answer.orchestration.CoverageKind.THIS_MESSAGE`),
    so it re-reacts instead of dispatching twice.

    No prose is posted on a fresh dispatch (#1155): the user reads Slack DMs
    only, every thread reply is a phone notification, and 🔧 already says a lane
    exists. A COVERED request does post, because which lane has it is
    information the reaction cannot carry and the owner would otherwise wait for.
    """
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.DELEGATED):
        return
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
            ),
        )
    try:
        backend.react(channel=unit.channel, ts=unit.slack_ts, emoji=InboundReaction.IN_FLIGHT)
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    report.dispatched += 1


def _report_coverage(backend: MessagingBackend, unit: _Unit, coverage: Coverage) -> bool:
    """Tell the owner which lane already has this; ``True`` once it is delivered.

    Same post/read-back discipline as the answer path, keyed on the thread ROOT
    (#2061), so the message is confirmed visible before the claim is kept. The
    unit is then reacted 🔧 IN FLIGHT — the covering lane is running, which is
    not the same as finished.
    """
    thread_root = resolve_thread_root(backend, channel=unit.channel, ts=unit.slack_ts)
    if not bot_reply_present_in_thread(backend, channel=unit.channel, thread_root=thread_root):
        backend.post_reply(
            channel=unit.channel,
            ts=unit.slack_ts,
            text=f"Already covered by {coverage.describe()} — not dispatching a second lane.",
        )
        if not verify_reply_visible(backend, channel=unit.channel, thread_root=thread_root):
            return False
    backend.react(channel=unit.channel, ts=unit.slack_ts, emoji=InboundReaction.IN_FLIGHT)
    return True


def _answer_bound_question(backend: MessagingBackend, unit: _Unit, reader: InboundReader) -> bool:
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
    bound = bind_reply(unit.lead, reader=reader, text=unit.text)
    if bound is None:
        return False
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.QUESTION_REPLY):
        return False
    if not apply_bound_answer(bound):
        _unmark_unit_loop_replied(unit)
        return False
    _react_done(backend, unit)
    return True


def _memoized(reader: InboundReader) -> InboundReader:
    """One reading per text — the binder's question-guard and the router share it."""
    cache: dict[str, InboundReading] = {}

    def read(text: str) -> InboundReading:
        if text not in cache:
            cache[text] = reader(text)
        return cache[text]

    return read


def _process_unit(
    backend: MessagingBackend,
    unit: _Unit,
    report: SlackAnswerReport,
    reader: InboundReader,
) -> None:
    if _react_eyes_once(backend, unit):
        report.eyes_reacted += 1

    read = _memoized(reader)
    if _answer_bound_question(backend, unit, read):
        report.answered_question += 1
        return

    reading = read(unit.text)
    if reading.needs_nothing:
        if _handle_noted(backend, unit):
            report.acked += 1
        return
    if reading.intent is InboundIntent.QUESTION and reading.answerable:
        outcome = _handle_simple(backend, unit)
        if outcome == "simple":
            report.answered_simple += 1
            return
        if outcome == "retry":
            return  # leave unanswered, retry next cycle
        # else: the cheap answer bailed → the question needs a lane after all
    _orchestrate(backend, unit, reading, report)


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
