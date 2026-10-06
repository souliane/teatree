"""``ticket rework-hold`` re-queues the findings of a HOLD a ticket was parked past."""

import json
from io import StringIO
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import ticket as ticket_mod
from teatree.core.management.refusal_exit import REFUSAL_EXIT_CODE
from teatree.core.modelkit.task_failure_taxonomy import SUPERSEDED_PREFIX
from teatree.core.models import PullRequest, Task, Ticket
from tests.teatree_core._self_review_helpers import HELD_FINDINGS, HELD_SHA, author_ticket, completed_self_review


def _rework_hold(ticket: Ticket, *args: str) -> dict[str, object]:
    return cast("dict[str, object]", call_command("ticket", "rework-hold", str(ticket.pk), *args))


def _parked_past_a_hold() -> tuple[Ticket, Task, Task]:
    ticket = author_ticket(state=Ticket.State.SELF_REVIEWED)
    held = completed_self_review(ticket, "hold")
    return ticket, held, ticket.schedule_shipping(parent_task=held)


def _task_rows(ticket: Ticket) -> list[tuple[int, str, str]]:
    return list(Task.objects.filter(ticket=ticket).order_by("pk").values_list("pk", "phase", "status"))


class TestReworkHoldRequeuesTheFindings(TestCase):
    def test_it_supersedes_shipping_and_queues_one_rework_whose_completion_re_tests(self) -> None:
        ticket, held, shipping = _parked_past_a_hold()

        result = _rework_hold(ticket)

        shipping.refresh_from_db()
        assert shipping.status == Task.Status.FAILED
        assert shipping.failure_reason.startswith(SUPERSEDED_PREFIX)
        [rework] = Task.objects.filter(ticket=ticket, phase="coding")
        assert rework.parent_task_id == held.pk
        assert str(HELD_FINDINGS[0]["summary"]) in rework.execution_reason
        assert result == {
            "ticket_id": ticket.pk,
            "state": Ticket.State.SELF_REVIEWED,
            "held_task": held.pk,
            "reviewed_sha": HELD_SHA,
            "findings": len(HELD_FINDINGS),
            "rework_task": rework.pk,
            "dry_run": False,
        }

        rework.claim(claimed_by="coder")
        rework.complete()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        assert Task.objects.filter(ticket=ticket, phase="testing", parent_task=rework).exists()

    def test_a_second_run_returns_the_same_rework(self) -> None:
        ticket, _held, _shipping = _parked_past_a_hold()
        first = _rework_hold(ticket)

        second = _rework_hold(ticket)

        assert second["rework_task"] == first["rework_task"]
        assert Task.objects.filter(ticket=ticket, phase="coding").count() == 1

    def test_a_dry_run_writes_nothing(self) -> None:
        ticket, held, _shipping = _parked_past_a_hold()
        before = _task_rows(ticket)

        result = _rework_hold(ticket, "--dry-run")

        assert result["held_task"] == held.pk
        assert result["rework_task"] is None
        assert result["dry_run"] is True
        assert _task_rows(ticket) == before


class TestReworkHoldRefuses(TestCase):
    def _assert_refused_and_untouched(self, ticket: Ticket, needle: str) -> None:
        before = _task_rows(ticket)

        result = _rework_hold(ticket)

        assert needle in str(result["error"])
        assert result["hint"]
        assert _task_rows(ticket) == before
        with pytest.raises(SystemExit) as exc:
            ticket_mod.Command().run_from_argv(["manage.py", "ticket", "rework-hold", str(ticket.pk)])
        assert exc.value.code == REFUSAL_EXIT_CODE
        assert _task_rows(ticket) == before

    def test_a_merge_safe_latest_self_review(self) -> None:
        ticket, _held, _shipping = _parked_past_a_hold()
        completed_self_review(ticket, "merge_safe")

        self._assert_refused_and_untouched(ticket, "not a HOLD")

    def test_an_open_pull_request(self) -> None:
        ticket, _held, _shipping = _parked_past_a_hold()
        PullRequest.objects.create(
            ticket=ticket, overlay="test", url="https://github.com/souliane/teatree/pull/9", repo="souliane/teatree"
        )

        self._assert_refused_and_untouched(ticket, "open pull request")

    def test_a_ticket_still_short_of_testing(self) -> None:
        ticket = author_ticket(state=Ticket.State.CODED)
        completed_self_review(ticket, "hold")

        self._assert_refused_and_untouched(ticket, "coded")

    def test_an_unknown_ticket(self) -> None:
        result = cast("dict[str, object]", call_command("ticket", "rework-hold", "987654"))

        assert result["error"] == "Ticket 987654 not found"

    def test_a_reviewer_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.REVIEWER, state=Ticket.State.TESTED)

        self._assert_refused_and_untouched(ticket, "reviewer")


class TestReworkHoldOutput(TestCase):
    def test_json_goes_to_stdout(self) -> None:
        ticket, held, _shipping = _parked_past_a_hold()
        out = StringIO()

        call_command("ticket", "rework-hold", str(ticket.pk), "--dry-run", "--json", stdout=out)

        assert json.loads(out.getvalue())["held_task"] == held.pk

    def test_the_human_view_names_the_rework_or_the_refusal(self) -> None:
        ticket, _held, _shipping = _parked_past_a_hold()
        dry, done, refused = StringIO(), StringIO(), StringIO()

        call_command("ticket", "rework-hold", str(ticket.pk), "--dry-run", stderr=dry)
        call_command("ticket", "rework-hold", str(ticket.pk), stderr=done)
        call_command("ticket", "rework-hold", "987654", stderr=refused)

        rework = Task.objects.get(ticket=ticket, phase="coding")
        assert "would queue a rework task" in dry.getvalue()
        assert f"rework task {rework.pk} carrying {len(HELD_FINDINGS)} finding(s)" in done.getvalue()
        assert "rework-hold refused: Ticket 987654 not found" in refused.getvalue()
