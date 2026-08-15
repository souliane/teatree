"""``t3 <overlay> cycle_time`` — the read front door over :mod:`teatree.core.cycle_time` (#4480).

Two questions, two verbs: ``ticket`` answers "where did this ticket's time go", and
``distribution`` answers "which edge dominates, and how wide is its tail". Nothing is
computed here — this positions figures the core layer produced, the same relationship
``teatree.dash.cycle_time`` has to it, so the caveat that layer is built around
(an attempt's start stamp is written at completion, so it is not a start time) holds by
construction rather than by repetition.

A duration the data cannot support is reported as UNKNOWN — ``null`` on the wire — never
as a plausible zero: an invented figure is worse than a gap, because it gets optimised
against. That applies to a segment whose queue/work split has no admission stamp behind
it, to the ticket totals such a segment makes a lower bound, and to a ticket with fewer
than two transitions, which measures nothing at all.

Read-only: every query underneath is a select. Scope rides ``T3_OVERLAY_NAME`` (the
``t3 <overlay>`` bridge sets it) and is stamped in the payload, so a consumer can tell an
overlay-scoped reading from a global one from the output alone.
"""

import os
from datetime import datetime, timedelta
from typing import IO, TYPE_CHECKING, Annotated, NoReturn, TypedDict, cast

import typer
from django.utils import timezone
from django_typer.management import command, initialize

from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.models.ticket import Ticket
from teatree.core.selectors._helpers import _humanize_duration
from teatree.core.table_output import print_table

if TYPE_CHECKING:
    from teatree.core.cycle_time import PhaseSegment, TicketTimeline, TransitionStat

DEFAULT_WINDOW_DAYS = 7
UNKNOWN = "UNKNOWN"


class Refusal(TypedDict):
    error: str
    hint: str


class SegmentPayload(TypedDict):
    from_state: str
    to_state: str
    phase: str
    entered_at: str
    left_at: str
    seconds: float
    queue_seconds: float | None
    work_seconds: float | None
    work_measured: bool
    attempts: int
    cost_usd: float
    cost_estimated_usd: float


class TicketCyclePayload(TypedDict):
    ticket_id: int
    number: str
    overlay: str
    state: str
    #: False when the ticket has no measurable span at all; the durations are then null.
    measured: bool
    started_at: str | None
    ended_at: str | None
    lead_time_seconds: float | None
    queue_seconds: float | None
    work_seconds: float | None
    work_measured: bool
    cost_usd: float
    cost_estimated_usd: float
    segments: list[SegmentPayload]


class EdgePayload(TypedDict):
    from_state: str
    to_state: str
    samples: int
    median_seconds: float
    p90_seconds: float


class DistributionPayload(TypedDict):
    #: The scope this reading was computed under — "" is a global read, not a missing one.
    overlay: str
    window_days: int
    since: str
    samples: int
    edges: list[EdgePayload]


class Command(MachineOutputCommand):
    """Read-only cycle-time measurement: where intake-to-merge time actually goes."""

    @initialize()
    def init(self) -> None:
        """Group root — forces sub-commands to be addressed by name."""

    @command(name="ticket")
    def ticket(
        self,
        ticket_id: Annotated[str, typer.Argument(help="Ticket pk, issue number, issue URL, or repo#N.")],
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human view."),
        ] = False,
    ) -> TicketCyclePayload:
        """Report one ticket's per-phase durations, split into queue wait and agent work."""
        from teatree.core.cycle_time import build_ticket_timeline  # noqa: PLC0415 — deferred: ORM/app-registry

        try:
            ticket = Ticket.objects.resolve(ticket_id)
        except Ticket.DoesNotExist:
            self._refuse(
                Refusal(
                    error=f"No ticket resolves from {ticket_id!r} (tried pk, issue number, issue URL, repo key).",
                    hint="t3 <overlay> ticket list — or pass the internal DB pk.",
                ),
                json_output=json_output,
            )

        timeline = build_ticket_timeline(ticket.pk)
        payload = _ticket_payload(ticket, timeline)
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: _render_timeline(payload, stream=stream),
        )
        return payload

    @command(name="distribution")
    def distribution(
        self,
        *,
        window_days: Annotated[
            int,
            typer.Option("--window-days", help=f"Trailing window width in days (default {DEFAULT_WINDOW_DAYS})."),
        ] = DEFAULT_WINDOW_DAYS,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human view."),
        ] = False,
    ) -> DistributionPayload:
        """Report median and p90 per transition edge over the trailing window, slowest first."""
        from teatree.core.cycle_time import transition_distribution  # noqa: PLC0415 — deferred: ORM/app-registry

        if window_days < 1:
            # Refused rather than clamped: silently substituting a window the operator did
            # not ask for is the same invented figure this command exists to avoid.
            self._refuse(
                Refusal(
                    error=f"--window-days must be at least 1 (got {window_days}).",
                    hint="t3 <overlay> cycle_time distribution --window-days 7",
                ),
                json_output=json_output,
            )

        overlay = os.environ.get("T3_OVERLAY_NAME", "")
        since = timezone.now() - timedelta(days=window_days)
        stats = transition_distribution(since=since, overlay=overlay)
        payload = DistributionPayload(
            overlay=overlay,
            window_days=window_days,
            since=since.isoformat(),
            samples=sum(stat.samples for stat in stats),
            edges=[_edge_payload(stat) for stat in stats],
        )
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=lambda stream: _render_distribution(payload, stream=stream),
        )
        return payload

    def _refuse(self, refusal: Refusal, *, json_output: bool) -> NoReturn:
        """Emit the refusal on the channel the caller is reading, then fail loudly.

        The payload is written BEFORE the raise: a bare non-zero exit with both streams
        empty is worse than the exit 0 it replaces — the operator gets no reason and a
        machine consumer loses the structured error entirely.
        """
        self.print_result = False
        emit(
            refusal,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"{refusal['error']}\n{refusal['hint']}\n",
        )
        raise SystemExit(1)


def _ticket_payload(ticket: Ticket, timeline: "TicketTimeline") -> TicketCyclePayload:
    measured = bool(timeline.segments)
    return TicketCyclePayload(
        ticket_id=timeline.ticket_id,
        number=timeline.number,
        overlay=ticket.overlay,
        state=timeline.state,
        measured=measured,
        started_at=_stamp(timeline.started_at),
        ended_at=_stamp(timeline.ended_at),
        lead_time_seconds=timeline.lead_time_seconds if measured else None,
        queue_seconds=_split(timeline.queue_seconds, known=measured and timeline.work_measured),
        work_seconds=_split(timeline.work_seconds, known=measured and timeline.work_measured),
        work_measured=timeline.work_measured,
        cost_usd=round(timeline.cost_usd, 4),
        cost_estimated_usd=round(timeline.cost_estimated_usd, 4),
        segments=[_segment_payload(segment) for segment in timeline.segments],
    )


def _segment_payload(segment: "PhaseSegment") -> SegmentPayload:
    return SegmentPayload(
        from_state=segment.from_state,
        to_state=segment.to_state,
        phase=segment.phase,
        entered_at=segment.entered_at.isoformat(),
        left_at=segment.left_at.isoformat(),
        seconds=segment.seconds,
        queue_seconds=_split(segment.queue_seconds, known=segment.work_measured),
        work_seconds=_split(segment.work_seconds, known=segment.work_measured),
        work_measured=segment.work_measured,
        attempts=segment.attempts,
        cost_usd=round(segment.cost_usd, 4),
        cost_estimated_usd=round(segment.cost_estimated_usd, 4),
    )


def _edge_payload(stat: "TransitionStat") -> EdgePayload:
    return EdgePayload(
        from_state=stat.from_state,
        to_state=stat.to_state,
        samples=stat.samples,
        median_seconds=stat.median_seconds,
        p90_seconds=stat.p90_seconds,
    )


def _split(seconds: float, *, known: bool) -> float | None:
    """A split half, or ``None`` where the core layer's 0.0 means UNKNOWN rather than zero."""
    return seconds if known else None


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _duration(seconds: float | None) -> str:
    return UNKNOWN if seconds is None else _humanize_duration(seconds)


def _render_timeline(payload: TicketCyclePayload, *, stream: IO[str]) -> None:
    """Both views render from the same payload, so the table and the JSON cannot disagree."""
    title = f"Ticket {payload['number']} ({payload['overlay'] or 'global'}) — {payload['state']}"
    if not payload["measured"]:
        stream.write(f"{title}\nNo measured span yet — a ticket's first transition measures nothing.\n")
        return
    stream.write(
        f"{title}\n"
        f"lead time {_duration(payload['lead_time_seconds'])} · "
        f"waiting {_duration(payload['queue_seconds'])} · "
        f"working {_duration(payload['work_seconds'])}\n"
    )
    print_table(
        ["Edge", "Phase", "Elapsed", "Waiting", "Working", "Attempts"],
        [
            [
                f"{row['from_state']} → {row['to_state']}",
                row["phase"] or "—",
                _humanize_duration(row["seconds"]),
                _duration(row["queue_seconds"]),
                _duration(row["work_seconds"]),
                str(row["attempts"]),
            ]
            for row in payload["segments"]
        ],
        stream=stream,
    )


def _render_distribution(payload: DistributionPayload, *, stream: IO[str]) -> None:
    scope = payload["overlay"] or "global"
    if not payload["edges"]:
        stream.write(f"No measured spans in the last {payload['window_days']}d ({scope}).\n")
        return
    print_table(
        ["Edge", "Samples", "Median", "p90"],
        [
            [
                f"{row['from_state']} → {row['to_state']}",
                str(row["samples"]),
                _humanize_duration(row["median_seconds"]),
                _humanize_duration(row["p90_seconds"]),
            ]
            for row in payload["edges"]
        ],
        title=f"Cycle time — last {payload['window_days']}d ({scope}), {payload['samples']} spans",
        stream=stream,
    )
