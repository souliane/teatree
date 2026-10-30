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
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.agents.data_fence import fenced
from teatree.core.managers_issue_match import reference_issue_q
from teatree.core.models import Session, SessionCommand, Task, ThreadBinding, Ticket
from teatree.core.models.question_text import question_fingerprint
from teatree.core.models.thread_binding import slack_command_id
from teatree.loop.inbound_reading import InboundReading
from teatree.url_classify import find_forge_refs
from teatree.utils.url_slug import slack_conversation_anchor

if TYPE_CHECKING:
    from teatree.loop.slack_answer.coalescing import _Unit

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

#: ``Ticket.short_description``'s own column width — the lane's card label is
#: truncated to it rather than to a second hand-maintained number.
_DESCRIPTION_LIMIT = 80


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
    thread_root: str = ""

    @classmethod
    def of_unit(cls, unit: "_Unit") -> "WorkOrigin":
        return cls(
            overlay=unit.overlay,
            channel=unit.channel,
            slack_ts=unit.slack_ts,
            coalesced_ts=tuple(row.slack_ts for row in unit.rows),
            text=unit.text,
            thread_root=unit.lead.thread_ts,
        )


def open_conversation(*, reading: InboundReading, fingerprint: str, origin: WorkOrigin, implies_work: bool) -> Ticket:
    """The bookkeeping row of *origin*'s Slack thread, bound to it; idempotent per message and per thread."""
    root = origin.thread_root or origin.slack_ts
    subject = (reading.work_summary or origin.text).strip()
    ticket, _created = Ticket.objects.get_or_create(
        issue_url=slack_conversation_anchor(channel=origin.channel, slack_ts=origin.slack_ts),
        defaults={
            "overlay": origin.overlay,
            "role": Ticket.Role.AUTHOR,
            "short_description": subject[:_DESCRIPTION_LIMIT],
            "extra": {
                _SLACK_ANSWER_KEY: {
                    "channel": origin.channel,
                    "slack_ts": root,
                    "coalesced_ts": list(origin.coalesced_ts),
                    "question": origin.text,
                    "fingerprint": fingerprint,
                    "intent": str(reading.intent),
                    "implies_work": implies_work,
                    "work_summary": reading.work_summary,
                }
            },
        },
    )
    if ticket.overlay != origin.overlay:
        Ticket.objects.filter(pk=ticket.pk).update(overlay=origin.overlay)
        ticket.overlay = origin.overlay
    ThreadBinding.bind(ticket=ticket, channel=origin.channel, thread_ts=root)
    return ticket


def bind_first_contact(unit: "_Unit", reading: InboundReading, binding: ThreadBinding | None) -> None:
    """Give an answered-in-place message's thread a conversation row, so the next reply routes by its binding."""
    if binding is None:
        open_conversation(
            reading=reading,
            fingerprint=work_fingerprint(reading, unit.text),
            origin=WorkOrigin.of_unit(unit),
            implies_work=False,
        )


def dispatch_work(
    *,
    reading: InboundReading,
    fingerprint: str,
    origin: WorkOrigin,
) -> Task:
    """Mint ONE conversation lane for *text* through the ordinary Ticket/Session/Task path.

    Nothing here is fire-and-forget: the Task is PENDING and claimable by the
    loop's own ``claim-next``, the Session gives it an identity, and the Ticket
    carries the message's origin so a later cycle (or a human) can trace the lane
    back to the DM that caused it — and so :func:`find_coverage` recognises it.

    The Ticket is the lane's BOOKKEEPING row, not the tracked work item (#4527). It
    is anchored on the synthetic-loop umbrella and described, so it is unique per
    Slack message (the ts makes the dispatch idempotent under
    ``unique_nonempty_issue_url``), recognisable to the artifact-terminal task sweep,
    and never :meth:`~teatree.core.models.ticket.Ticket.is_admissible` — because at
    dispatch time nobody has yet decided WHAT the work is. The work item is the real
    forge issue the answering phase files through
    :func:`~teatree.core.answering.work_item_filing.file_work_item`; only that row is
    findable by intake, and only it may be announced to the owner.
    """
    overlay, channel, slack_ts, coalesced_ts, text = (
        origin.overlay,
        origin.channel,
        origin.slack_ts,
        origin.coalesced_ts,
        origin.text,
    )
    root = origin.thread_root or slack_ts
    with transaction.atomic():
        binding = (
            ThreadBinding.objects.select_related("ticket")
            .filter(overlay=overlay, channel=channel, thread_ts=root)
            .first()
        )
        if (
            binding is not None
            and (
                existing := SessionCommand.objects.filter(
                    command_id=slack_command_id(overlay, channel, slack_ts)
                ).first()
            )
            is not None
        ):
            if existing.ticket_id != binding.ticket_id or existing.text != text:
                msg = "A Slack message already belongs to another session command"
                raise ValueError(msg)
            return existing.task
        if binding is None:
            ticket = open_conversation(reading=reading, fingerprint=fingerprint, origin=origin, implies_work=True)
        else:
            ticket = binding.ticket
            extra = dict(ticket.extra or {})
            prior = dict(extra.get(_SLACK_ANSWER_KEY) or {})
            prior.update(
                channel=channel,
                slack_ts=root,
                coalesced_ts=sorted({*prior.get("coalesced_ts", []), *coalesced_ts, slack_ts}),
                question=text,
                fingerprint=fingerprint,
                intent=str(reading.intent),
                implies_work=True,
                work_summary=reading.work_summary,
            )
            extra[_SLACK_ANSWER_KEY] = prior
            ticket.extra = extra
            ticket.save(update_fields=["extra"])
        session = Session.objects.create(ticket=ticket, overlay=overlay, agent_id=DISPATCH_PHASE)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            subject=(reading.work_summary or text)[:120],
            phase=DISPATCH_PHASE,
            execution_reason=_execution_reason(reading, slack_ts=slack_ts, text=text),
        )
        if binding is not None:
            SessionCommand.objects.create(
                command_id=slack_command_id(overlay, channel, slack_ts),
                ticket=ticket,
                task=task,
                text=text,
                outcome="queued_followup",
            )
        return task


def _execution_reason(reading: InboundReading, *, slack_ts: str, text: str) -> str:
    summary = f"\nInterpreted as: {reading.work_summary}" if reading.work_summary else ""
    return f"[{reading.intent}] Owner Slack message at ts={slack_ts}:\n{fenced('OWNER MESSAGE', text)}{summary}"


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
    urls = find_forge_refs(text)
    if not urls:
        return None
    for url in urls:
        ticket = (
            Ticket.objects.filter(reference_issue_q(url), overlay=overlay)
            .exclude(state__in=Ticket._SETTLED_STATES)  # noqa: SLF001 — the model's SSOT terminal set
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
    "WorkOrigin",
    "bind_first_contact",
    "dispatch_work",
    "find_coverage",
    "open_conversation",
    "work_fingerprint",
]
