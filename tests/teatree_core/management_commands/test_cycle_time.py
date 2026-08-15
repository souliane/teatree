"""``t3 <overlay> cycle_time`` — the read front door over the measurement layer (#4480).

The measurement modules computed spans, timelines and distributions that no command could
read, so "where does intake-to-merge time go?" was answered from GitHub and from reading
transcripts by hand. These tests pin the answer's SHAPE, and above all pin that a duration
the data cannot support reports UNKNOWN rather than a plausible number — an invented
figure is worse than a gap, because it gets optimised against.
"""

import json
import os
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.management.commands import cycle_time as cycle_time_command
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from tests.teatree_core.cycle_time.test_timeline import MINUTE, record_attempt, record_task, record_transition

State = Ticket.State

DAY = 24 * 60


def _stdout(*args: str, **kwargs: object) -> str:
    """The machine channel — JSON under ``--json``, empty otherwise."""
    buf = StringIO()
    call_command("cycle_time", *args, stdout=buf, **kwargs)
    return buf.getvalue()


def _payload(*args: str, **kwargs: object) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(_stdout(*args, json_output=True, **kwargs)))


def _stderr(*args: str, **kwargs: object) -> str:
    """The human channel — where the seam routes the readable view."""
    buf = StringIO()
    call_command("cycle_time", *args, stderr=buf, **kwargs)
    return buf.getvalue()


def _scoped_payload(overlay: str, *args: str) -> dict[str, Any]:
    """The payload a ``t3 <overlay> cycle_time`` invocation produces (scope rides the env)."""
    with mock.patch.dict(os.environ, {"T3_OVERLAY_NAME": overlay}):
        return _payload(*args)


def edge_ending(
    *,
    overlay: str = "t3-teatree",
    source: str = State.PLANNED,
    target: str = State.CODED,
    minutes: float,
    ago: float = 60,
) -> Ticket:
    """A ticket whose only measurable span is *source* -> *target*, finishing *ago* minutes back.

    Stamped relative to now rather than to a fixed origin: the window verbs read backwards
    from the wall clock, so a fixture pinned to a calendar date would drift out of the
    window as the repo ages and red a passing lane months later.
    """
    ticket = Ticket.objects.create(state=target, overlay=overlay)
    # One clock read for both stamps: reading it per row leaves the span microseconds
    # longer than `minutes`, and every duration assertion below is exact.
    now = timezone.now()
    for (edge_from, edge_to), offset in (((State.NOT_STARTED, source), ago + minutes), ((source, target), ago)):
        row = TicketTransition.objects.create(
            ticket=ticket,
            from_state=edge_from,
            to_state=edge_to,
            triggered_by="test",
        )
        TicketTransition.objects.filter(pk=row.pk).update(created_at=now - timedelta(minutes=offset))
    return ticket


class TicketTimelineTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=State.CODED, overlay="t3-teatree")
        record_transition(cls.ticket, source=State.NOT_STARTED, target=State.STARTED, minutes=0)
        record_transition(cls.ticket, source=State.STARTED, target=State.PLANNED, minutes=10)
        record_transition(cls.ticket, source=State.PLANNED, target=State.CODED, minutes=70)
        record_attempt(record_task(cls.ticket, phase="coding", queued=10, admitted=20), ended=60)

    def _segment_to(self, state: str) -> dict[str, Any]:
        payload = _payload("ticket", str(self.ticket.pk))
        return next(row for row in payload["segments"] if row["to_state"] == state)

    def test_every_measured_span_is_reported_with_its_duration(self) -> None:
        payload = _payload("ticket", str(self.ticket.pk))
        assert [(row["from_state"], row["to_state"], row["seconds"]) for row in payload["segments"]] == [
            (State.STARTED, State.PLANNED, 10 * MINUTE),
            (State.PLANNED, State.CODED, 60 * MINUTE),
        ]

    def test_a_segment_names_the_phase_that_produced_its_target_state(self) -> None:
        assert self._segment_to(State.CODED)["phase"] == "coding"

    def test_a_measured_split_reports_both_halves(self) -> None:
        coding = self._segment_to(State.CODED)
        assert coding["work_measured"] is True
        assert coding["work_seconds"] == 40 * MINUTE
        assert coding["queue_seconds"] == 20 * MINUTE

    def test_the_ticket_carries_its_lead_time_and_identity(self) -> None:
        payload = _payload("ticket", str(self.ticket.pk))
        assert payload["lead_time_seconds"] == 70 * MINUTE
        assert payload["measured"] is True
        assert payload["overlay"] == "t3-teatree"
        assert payload["number"] == self.ticket.ticket_number

    def test_a_ticket_resolves_by_issue_number_not_just_by_pk(self) -> None:
        Ticket.objects.filter(pk=self.ticket.pk).update(issue_url="https://github.com/souliane/teatree/issues/99480")
        assert _payload("ticket", "99480")["ticket_id"] == self.ticket.pk

    def test_the_human_view_names_the_ticket_and_its_phases(self) -> None:
        out = _stderr("ticket", str(self.ticket.pk))
        assert self.ticket.ticket_number in out
        assert "coding" in out

    def test_the_machine_channel_is_pure_json_with_no_human_bytes(self) -> None:
        raw = _stdout("ticket", str(self.ticket.pk), json_output=True)
        assert raw.strip().startswith("{")
        assert json.loads(raw)["ticket_id"] == self.ticket.pk

    def test_the_human_run_leaves_the_machine_channel_empty(self) -> None:
        assert _stdout("ticket", str(self.ticket.pk)) == ""


class UnknownRatherThanZeroTestCase(TestCase):
    """The acceptance clause with teeth: a duration the data cannot support is UNKNOWN."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=State.CODED, overlay="t3-teatree")
        record_transition(cls.ticket, source=State.NOT_STARTED, target=State.PLANNED, minutes=0)
        record_transition(cls.ticket, source=State.PLANNED, target=State.CODED, minutes=60)
        # An agent demonstrably ran here (an attempt ENDED in the span) but no admission
        # stamp exists to measure the stretch with, so the split is unknown, not zero.
        record_attempt(record_task(cls.ticket, phase="coding", queued=0, admitted=None), ended=30)

    def test_an_unmeasurable_split_reports_null_never_zero(self) -> None:
        coding = _payload("ticket", str(self.ticket.pk))["segments"][0]
        assert coding["work_measured"] is False
        assert coding["work_seconds"] is None
        assert coding["queue_seconds"] is None

    def test_the_span_itself_is_still_reported(self) -> None:
        assert _payload("ticket", str(self.ticket.pk))["segments"][0]["seconds"] == 60 * MINUTE

    def test_unmeasured_totals_are_null_because_they_are_only_lower_bounds(self) -> None:
        payload = _payload("ticket", str(self.ticket.pk))
        assert payload["work_measured"] is False
        assert payload["work_seconds"] is None
        assert payload["queue_seconds"] is None

    def test_the_lead_time_survives_because_it_is_always_measurable(self) -> None:
        assert _payload("ticket", str(self.ticket.pk))["lead_time_seconds"] == 60 * MINUTE

    def test_the_human_view_spells_the_gap_out(self) -> None:
        assert "UNKNOWN" in _stderr("ticket", str(self.ticket.pk))


class NoMeasurableSpanTestCase(TestCase):
    """One transition opens a timeline and measures nothing — there is no honest zero."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=State.STARTED, overlay="t3-teatree")
        record_transition(cls.ticket, source=State.NOT_STARTED, target=State.STARTED, minutes=0)

    def test_the_lead_time_is_null_rather_than_zero(self) -> None:
        payload = _payload("ticket", str(self.ticket.pk))
        assert payload["measured"] is False
        assert payload["lead_time_seconds"] is None
        assert payload["segments"] == []

    def test_the_human_view_says_so_instead_of_drawing_an_empty_table(self) -> None:
        assert "no measured span" in _stderr("ticket", str(self.ticket.pk)).lower()


class DistributionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        for minutes in (10, 20, 30):
            edge_ending(minutes=minutes)
        edge_ending(source=State.CODED, target=State.TESTED, minutes=200)

    def _edges(self) -> list[dict[str, Any]]:
        return _payload("distribution")["edges"]

    def test_each_edge_carries_its_sample_count_median_and_p90(self) -> None:
        coded = next(row for row in self._edges() if row["to_state"] == State.CODED)
        assert coded["samples"] == 3
        assert coded["median_seconds"] == 20 * MINUTE
        assert coded["p90_seconds"] == 28 * MINUTE

    def test_the_whale_edge_leads_the_table(self) -> None:
        assert self._edges()[0]["to_state"] == State.TESTED

    def test_the_payload_stamps_the_window_and_total_samples(self) -> None:
        payload = _payload("distribution")
        assert payload["window_days"] == 7
        assert payload["samples"] == 4
        assert payload["since"]

    def test_a_narrower_window_drops_the_spans_that_ended_outside_it(self) -> None:
        assert _payload("distribution", "--window-days", "30")["samples"] == 4
        edge_ending(minutes=15, ago=40 * DAY)
        assert _payload("distribution", "--window-days", "30")["samples"] == 4
        assert _payload("distribution", "--window-days", "60")["samples"] == 5

    def test_the_human_view_renders_the_edges(self) -> None:
        assert State.CODED in _stderr("distribution")

    def test_the_machine_channel_is_pure_json_with_no_human_bytes(self) -> None:
        assert _stdout("distribution", json_output=True).strip().startswith("{")


class EmptyWindowTestCase(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        edge_ending(minutes=15, ago=90 * DAY)

    def test_a_window_with_no_spans_reports_none_rather_than_a_table_of_zeros(self) -> None:
        payload = _payload("distribution", "--window-days", "7")
        assert payload["edges"] == []
        assert payload["samples"] == 0

    def test_the_human_view_says_there_is_nothing_to_show(self) -> None:
        assert "no measured spans" in _stderr("distribution", "--window-days", "7").lower()


class OverlayScopeTestCase(TestCase):
    """Scope rides ``T3_OVERLAY_NAME``, exactly like ``signals`` — and is stamped in the payload."""

    @classmethod
    def setUpTestData(cls) -> None:
        edge_ending(overlay="t3-teatree", minutes=10)
        edge_ending(overlay="other-overlay", minutes=90)

    def test_a_scoped_read_excludes_another_overlays_spans(self) -> None:
        payload = _scoped_payload("t3-teatree", "distribution")
        assert payload["samples"] == 1
        assert payload["edges"][0]["median_seconds"] == 10 * MINUTE

    def test_an_unscoped_read_spans_every_overlay(self) -> None:
        assert _scoped_payload("", "distribution")["samples"] == 2

    def test_the_scope_is_stamped_so_a_consumer_can_tell_it_from_a_global_read(self) -> None:
        assert _scoped_payload("t3-teatree", "distribution")["overlay"] == "t3-teatree"
        assert _scoped_payload("", "distribution")["overlay"] == ""


class RefusalTestCase(TestCase):
    def test_an_unresolvable_ticket_exits_non_zero_with_an_actionable_payload(self) -> None:
        buf = StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("cycle_time", "ticket", "no-such-ticket", "--json", stdout=buf)
        assert exc.value.code == 1
        refusal = json.loads(buf.getvalue())
        assert "no-such-ticket" in refusal["error"]
        assert refusal["hint"]

    def test_a_window_below_one_day_is_refused_rather_than_silently_clamped(self) -> None:
        buf = StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("cycle_time", "distribution", "--window-days", "0", "--json", stdout=buf)
        assert exc.value.code == 1
        assert "window-days" in json.loads(buf.getvalue())["error"]


class ComputesNoDurationItselfTestCase(TestCase):
    """``TaskAttempt.started_at`` is stamped at INSERT, and the insert happens at completion.

    A duration built on it is fiction, which is why the core layer never reads it. This
    command inherits that by positioning figures the core layer produced and deriving none
    of its own, so the guard is that it reaches no timing model directly — reaching one is
    how a hand-rolled duration would get back in.
    """

    def test_the_command_reaches_no_timing_model_directly(self) -> None:
        source = Path(cycle_time_command.__file__).read_text(encoding="utf-8")
        assert "TaskAttempt" not in source
        assert "TicketTransition" not in source

    def test_the_command_sources_its_figures_from_the_measurement_layer(self) -> None:
        source = Path(cycle_time_command.__file__).read_text(encoding="utf-8")
        assert "from teatree.core.cycle_time import" in source
