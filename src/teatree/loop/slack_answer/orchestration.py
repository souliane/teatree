"""Dedupe first, then dispatch — the inbound loop's orchestration half.

Reading a message as "something must be done" is only half a decision. The other
half is whether anyone is already doing it. Minting a second lane for work a
live lane already owns is not a wasted token budget, it is two agents writing
the same files: in a shared checkout the loser's work is reverted by the
winner's own pre-commit stash, and the owner is told twice that his request was
picked up.

So every dispatch goes through :func:`find_coverage` first, and it resolves
against the control plane's own records — Tasks, their Tickets, their Sessions —
never against the text of the message. Three questions, cheapest first:

1. **Is this exact message already dispatched?** The ticket a dispatch mints
    records its ``slack_ts`` (and every coalesced follow-up ts), so a re-run of
    the cycle recognises its own lane instead of minting a rival. This is what
    makes the whole path safe to retry after a failed thread post.
2. **Is the same REQUEST already in flight?** Keyed on the interpreted
    ``work_summary``, not the raw words, so "the interest-rate rounding is still wrong"
    and "did anyone look at the interest-rate rounding yet" collapse onto one lane.
3. **Is the subject already a live ticket?** A forge URL in the message that the
    factory already tracks in a non-terminal state is covered by that ticket,
    whatever words the owner used around it.

Coverage found is REPORTED, never silently swallowed: the caller posts it in the
thread. Telling the owner "task 42 has this" is the information he actually
needs — it is what stops him waiting, or asking again.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.deferred_question import question_fingerprint
from teatree.loop.inbound_reading import InboundReading
from teatree.url_classify import find_forge_urls

logger = logging.getLogger(__name__)

#: The phase a DM-originated lane runs in. It routes to the bounded answerer
#: sub-agent, which is the factory's front door for anything the owner says
#: — it answers, or it files and dispatches onward.
DISPATCH_PHASE = "answering"

#: How many active tasks a coverage scan reads. Active tasks are the machine's
#: live lanes; there are tens at most, and an unbounded scan on the reactive
#: path is a latency risk nobody is watching for.
_ACTIVE_SCAN_LIMIT = 200

_SLACK_ANSWER_KEY = "slack_answer"


class CoverageKind(StrEnum):
    """Why an inbound request needs no new lane."""

    #: A lane already exists for THIS message — a re-run of the same cycle.
    THIS_MESSAGE = "this_message"
    #: A live lane is working the same interpreted request.
    SAME_REQUEST = "same_request"
    #: The forge subject named in the message is a live ticket already.
    SAME_SUBJECT = "same_subject"


@dataclass(frozen=True, slots=True)
class Coverage:
    """The existing lane that makes a new dispatch unnecessary."""

    kind: CoverageKind
    reference: str
    subject: str = ""

    @property
    def is_this_messages_own_lane(self) -> bool:
        """Whether this coverage IS the lane minted for the message being processed.

        The caller reports it as a fresh dispatch rather than as a rival, because
        it is: the previous attempt created the lane and failed to post about it.
        """
        return self.kind is CoverageKind.THIS_MESSAGE

    def describe(self) -> str:
        subject = f" — {self.subject}" if self.subject else ""
        return f"{self.reference}{subject}"


def work_fingerprint(reading: InboundReading, text: str) -> str:
    """The dedupe key for a request: its interpreted summary, else its raw text.

    Keying on the model's normalised ``work_summary`` is what makes the dedupe
    resolve two differently-worded reports of one problem onto one lane. The raw
    text is the fallback for a heuristic reading, which has no summary to offer;
    it dedupes only verbatim repeats, which is still better than nothing.
    """
    return question_fingerprint(reading.work_summary or text)


def find_coverage(
    *,
    fingerprint: str,
    slack_ts: str,
    coalesced_ts: tuple[str, ...],
    overlay: str,
    text: str,
) -> Coverage | None:
    """The live lane that already covers this request, or ``None``.

    Reads only committed control-plane state, so it is safe to call from any
    cycle at any time; it creates nothing and stamps nothing.
    """
    own_ts = {slack_ts, *coalesced_ts} - {""}
    for task in _active_tasks(overlay):
        origin = _slack_origin(task.ticket)
        if own_ts & _recorded_ts(origin):
            return Coverage(
                kind=CoverageKind.THIS_MESSAGE,
                reference=_task_reference(task),
                subject=_origin_subject(origin),
            )
        if fingerprint and origin.get("fingerprint") == fingerprint:
            return Coverage(
                kind=CoverageKind.SAME_REQUEST,
                reference=_task_reference(task),
                subject=_origin_subject(origin),
            )
    return _subject_coverage(text=text, overlay=overlay)


@dataclass(frozen=True, slots=True)
class WorkOrigin:
    """Where a dispatched lane came from — the Slack message that caused it.

    Grouped because these five always travel together and mean nothing apart: they
    are one message's identity, not five independent knobs.
    """

    overlay: str
    channel: str
    slack_ts: str
    coalesced_ts: tuple[str, ...]
    text: str


def dispatch_work(
    *,
    reading: InboundReading,
    fingerprint: str,
    origin: WorkOrigin,
) -> Task:
    """Mint ONE tracked lane for *text* through the ordinary Ticket/Session/Task path.

    Nothing here is fire-and-forget: the Task is PENDING and claimable by the
    loop's own ``claim-next``, the Session gives it an identity, and the Ticket
    carries the message's origin so a later cycle (or a human) can trace the lane
    back to the DM that caused it — and so :func:`find_coverage` recognises it.
    """
    overlay, channel, slack_ts, coalesced_ts, text = (
        origin.overlay,
        origin.channel,
        origin.slack_ts,
        origin.coalesced_ts,
        origin.text,
    )
    ticket = Ticket.objects.create(
        overlay=overlay,
        role=Ticket.Role.AUTHOR,
        extra={
            _SLACK_ANSWER_KEY: {
                "channel": channel,
                "slack_ts": slack_ts,
                "coalesced_ts": list(coalesced_ts),
                "question": text,
                "fingerprint": fingerprint,
                "intent": str(reading.intent),
                "work_summary": reading.work_summary,
            }
        },
    )
    session = Session.objects.create(ticket=ticket, overlay=overlay, agent_id=DISPATCH_PHASE)
    return Task.objects.create(
        ticket=ticket,
        session=session,
        subject=(reading.work_summary or text)[:120],
        phase=DISPATCH_PHASE,
        execution_reason=_execution_reason(reading, slack_ts=slack_ts, text=text),
    )


def _execution_reason(reading: InboundReading, *, slack_ts: str, text: str) -> str:
    summary = f"\nInterpreted as: {reading.work_summary}" if reading.work_summary else ""
    return f"[{reading.intent}] Owner Slack message at ts={slack_ts}: {text}{summary}"


def _active_tasks(overlay: str) -> list[Task]:
    return list(
        Task.objects.filter(status__in=Task.Status.active(), ticket__overlay=overlay)
        .select_related("ticket")
        .order_by("-pk")[:_ACTIVE_SCAN_LIMIT]
    )


def _slack_origin(ticket: Ticket) -> dict:
    origin = ticket.extra.get(_SLACK_ANSWER_KEY) if isinstance(ticket.extra, dict) else None
    return origin if isinstance(origin, dict) else {}


def _recorded_ts(origin: dict) -> set[str]:
    coalesced = origin.get("coalesced_ts")
    recorded = set(coalesced) if isinstance(coalesced, list) else set()
    lead = origin.get("slack_ts")
    if isinstance(lead, str) and lead:
        recorded.add(lead)
    return {ts for ts in recorded if isinstance(ts, str) and ts}


def _origin_subject(origin: dict) -> str:
    summary = origin.get("work_summary") or origin.get("question") or ""
    return str(summary)[:120]


def _task_reference(task: Task) -> str:
    return f"task {task.pk} ({task.phase or 'unphased'})"


def _subject_coverage(*, text: str, overlay: str) -> Coverage | None:
    """A live ticket for a forge URL named in *text*.

    A ticket the factory is already carrying through its lifecycle covers every
    request about that subject, even one with no task claimed at this instant —
    a ticket between phases is still someone's work, and dispatching alongside it
    is exactly the rival lane this function exists to prevent.
    """
    urls = find_forge_urls(text)
    if not urls:
        return None
    for url in urls:
        ticket = (
            Ticket.objects.filter(overlay=overlay, issue_url=url)
            .exclude(state__in=Ticket._TERMINAL_STATES)  # noqa: SLF001 — the model's SSOT terminal set
            .order_by("-pk")
            .first()
        )
        if ticket is not None:
            return Coverage(
                kind=CoverageKind.SAME_SUBJECT,
                reference=f"ticket {ticket.pk} ({ticket.state})",
                subject=url,
            )
    return None


__all__ = [
    "DISPATCH_PHASE",
    "Coverage",
    "CoverageKind",
    "dispatch_work",
    "find_coverage",
    "work_fingerprint",
]
