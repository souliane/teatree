"""The single writer of ``Ticket.short_description`` — one cheap-tier turn, safe fallbacks (#1156).

The model turn is the one unstoppable external here, so it is patched; everything
else (the Ticket row, the persistence, the phase-runner outcome line) runs for real.
The turn returning ``None`` (no binary, timeout, backend error) must degrade to a
truncation fallback rather than leaving the field blank forever.
"""

from unittest import mock

import pytest
from django.test import TestCase

from teatree.agents import ticket_short_description
from teatree.agents.ticket_short_description import (
    TicketNotFoundError,
    describe_ticket_short_description,
    generate_short_description,
    run_short_describe,
)
from teatree.core.models import Session, Task, Ticket

_RUN_ONE_SHOT = "teatree.agents.ticket_short_description.run_one_shot"


class TestGenerateShortDescription:
    def test_a_blank_title_yields_empty(self) -> None:
        assert generate_short_description("   ") == ""

    def test_the_model_summary_is_used_when_present(self) -> None:
        with mock.patch(_RUN_ONE_SHOT, return_value="dark mode toggle"):
            assert generate_short_description("Add a dark mode toggle to settings") == "dark mode toggle"

    def test_the_last_nonblank_line_is_taken_and_quotes_stripped(self) -> None:
        with mock.patch(_RUN_ONE_SHOT, return_value='chatter\n"the summary"'):
            assert generate_short_description("Some ticket") == "the summary"

    def test_an_unavailable_model_degrades_to_a_truncation_fallback(self) -> None:
        # ``run_one_shot`` returns None (no binary / timeout / backend error) → the field
        # is still populated from the title rather than left blank.
        long_title = "x" * 60
        with mock.patch(_RUN_ONE_SHOT, return_value=None):
            summary = generate_short_description(long_title)
        assert summary.endswith("…")
        assert len(summary) == 40

    def test_a_short_title_falls_back_verbatim(self) -> None:
        with mock.patch(_RUN_ONE_SHOT, return_value=""):
            assert generate_short_description("short title") == "short title"


class TestDescribeTicketShortDescription(TestCase):
    def _ticket(self, title: str | None = "Add a dark mode toggle") -> Ticket:
        extra = {"issue_title": title} if title is not None else {}
        return Ticket.objects.create(overlay="t3-teatree", extra=extra)

    def test_a_missing_ticket_raises(self) -> None:
        with pytest.raises(TicketNotFoundError, match="id=999999"):
            describe_ticket_short_description(999999)

    def test_a_ticket_with_no_cached_title_is_left_untouched(self) -> None:
        ticket = self._ticket(title=None)
        assert describe_ticket_short_description(ticket.pk) == ""
        ticket.refresh_from_db()
        assert not ticket.short_description

    def test_a_title_is_summarized_and_persisted(self) -> None:
        ticket = self._ticket()
        with mock.patch(_RUN_ONE_SHOT, return_value="dark mode toggle"):
            summary = describe_ticket_short_description(ticket.pk)
        assert summary == "dark mode toggle"
        ticket.refresh_from_db()
        assert ticket.short_description == "dark mode toggle"


class TestRunShortDescribe(TestCase):
    def _task(self, title: str | None) -> Task:
        extra = {"issue_title": title} if title is not None else {}
        ticket = Ticket.objects.create(overlay="t3-teatree", extra=extra)
        session = Session.objects.create(ticket=ticket, agent_id="short-describe")
        return Task.objects.create(ticket=ticket, session=session, phase="short_describe")

    def test_a_ticket_with_a_title_reports_ok_with_the_summary(self) -> None:
        task = self._task("Add a dark mode toggle")
        with mock.patch(_RUN_ONE_SHOT, return_value="dark mode toggle"):
            outcome = run_short_describe(task)
        assert outcome.startswith("OK")
        assert "dark mode toggle" in outcome

    def test_a_ticket_with_no_title_reports_a_noop_skip(self) -> None:
        task = self._task(title=None)
        outcome = run_short_describe(task)
        assert outcome.startswith("NOOP")
        assert "issue_title" in outcome


class TestModuleExports:
    def test_the_public_seam_symbols_are_exported(self) -> None:
        assert set(ticket_short_description.__all__) == {
            "TicketNotFoundError",
            "describe_ticket_short_description",
            "generate_short_description",
            "run_short_describe",
        }
