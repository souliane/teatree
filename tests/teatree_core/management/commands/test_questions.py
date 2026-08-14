"""``t3 <overlay> questions reachability`` — the operator view of the drain gap (#4178).

The backlog reached 70 pending rows with a single automated drain covering 6, and
nothing surfaced that. This subcommand is the measurement: per pending row, which
resolvers can decide it, and how many can be decided by none.
"""

import io
import json

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion


class TestQuestionsReachability(TestCase):
    def test_json_reports_the_resolver_per_pending_row(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.FAILED)
        parked = DeferredQuestion.record("How should this park proceed?", parked_task=task)
        subjectless = DeferredQuestion.record("Which colour should the button be?")

        out = io.StringIO()
        call_command("questions", "reachability", "--json", stdout=out)

        payload = json.loads(out.getvalue())

        by_id = {row["id"]: row for row in payload}
        assert by_id[parked.pk]["has_subject"]
        assert by_id[parked.pk]["resolvers"]["subject_terminal"] == "keep"
        assert not by_id[subjectless.pk]["has_subject"]
        assert by_id[subjectless.pk]["resolvers"] == {}

    def test_human_view_counts_the_rows_no_resolver_reaches(self) -> None:
        DeferredQuestion.record("Which colour should the button be?")
        err = io.StringIO()

        call_command("questions", "reachability", stderr=err)

        # The count is the headline the operator acts on; the table itself is print_table's.
        assert "1 reachable by no resolver" in err.getvalue()


class TestQuestionsListShowsEscalations(TestCase):
    """An age-backstop escalation is readable on the operator's own surface (#4178)."""

    def test_json_carries_the_escalation_stamp(self) -> None:
        row = DeferredQuestion.record("Merge it?")
        assert row.mark_escalated("pending past the ceiling")

        out = io.StringIO()
        call_command("questions", "list", "--json", stdout=out)

        payload = {entry["id"]: entry for entry in json.loads(out.getvalue())}
        assert payload[row.pk]["escalation_count"] == 1
        assert payload[row.pk]["escalated_at"] is not None

    def test_an_unescalated_row_reports_no_stamp(self) -> None:
        row = DeferredQuestion.record("Merge it?")

        out = io.StringIO()
        call_command("questions", "list", "--json", stdout=out)

        payload = {entry["id"]: entry for entry in json.loads(out.getvalue())}
        assert payload[row.pk]["escalation_count"] == 0
        assert payload[row.pk]["escalated_at"] is None

    def test_human_view_counts_the_escalated_rows(self) -> None:
        row = DeferredQuestion.record("Merge it?")
        DeferredQuestion.record("And this one?")
        assert row.mark_escalated("pending past the ceiling")
        err = io.StringIO()

        call_command("questions", "list", stderr=err)

        assert "1 past the age ceiling" in err.getvalue()
