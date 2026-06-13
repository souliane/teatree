"""``review record`` advances an open EXTERNAL ReviewLoop (teatree#2298).

The pre-#2298 chokepoint left a HOLD verdict inert: ``_trigger_sweep``
early-returns on a non-merge-safe verdict, so a HOLD never fed back. The new
``_advance_review_loop`` step drives the bound EXTERNAL loop from the recorded
verdict — HOLD re-arms an author leg (or exhausts), MERGE_SAFE terminates at
PASSED — so the verdict actually moves the loop.
"""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ReviewLoop, Task, Ticket

pytestmark = pytest.mark.django_db

_SLUG = "acme/repo"
_PR_ID = 7
_HEAD = "a" * 40


def _external_loop_in_reviewing(*, ticket: Ticket, helper: TestCase, max_rounds: int = 3) -> ReviewLoop:
    with helper.captureOnCommitCallbacks(execute=True):
        loop = ReviewLoop.start_external_loop(ticket=ticket, max_rounds=max_rounds)
    task = loop.current_task
    assert task is not None
    task.status = Task.Status.COMPLETED
    task.save(update_fields=["status"])
    with helper.captureOnCommitCallbacks(execute=True):
        loop.submit_for_review()
        loop.save()
    loop.refresh_from_db()
    return loop


def _record(*, ticket: Ticket, verdict: str, findings_json: str = "") -> dict[str, object]:
    return cast(
        "dict[str, object]",
        call_command(
            "review",
            "record",
            str(_PR_ID),
            _SLUG,
            reviewed_sha=_HEAD,
            verdict=verdict,
            reviewer_identity="cold-reviewer",
            findings_json=findings_json,
            ticket_id=ticket.pk,
        ),
    )


class TestRecordAdvancesExternalLoop(TestCase):
    def test_record_hold_verdict_advances_external_loop_to_authoring(self) -> None:
        ticket = Ticket.objects.create()
        loop = _external_loop_in_reviewing(ticket=ticket, helper=self)

        with self.captureOnCommitCallbacks(execute=True):
            _record(
                ticket=ticket,
                verdict="hold",
                findings_json='[{"severity":"major","summary":"missing assertion"}]',
            )

        loop.refresh_from_db()
        assert loop.state == ReviewLoop.State.AUTHORING
        assert loop.round == 1
        assert ticket.tasks.filter(phase="e2e").count() == 2

    def test_record_merge_safe_verdict_advances_external_loop_to_passed(self) -> None:
        ticket = Ticket.objects.create()
        loop = _external_loop_in_reviewing(ticket=ticket, helper=self)

        with self.captureOnCommitCallbacks(execute=True):
            _record(ticket=ticket, verdict="merge_safe")

        loop.refresh_from_db()
        assert loop.state == ReviewLoop.State.PASSED
        assert loop.passed is True

    def test_record_hold_at_last_round_exhausts(self) -> None:
        ticket = Ticket.objects.create()
        loop = _external_loop_in_reviewing(ticket=ticket, helper=self, max_rounds=1)

        with self.captureOnCommitCallbacks(execute=True):
            _record(ticket=ticket, verdict="hold")

        loop.refresh_from_db()
        assert loop.state == ReviewLoop.State.EXHAUSTED
        assert loop.needs_user_input is True

    def test_record_with_no_open_loop_is_a_no_op(self) -> None:
        ticket = Ticket.objects.create()

        with self.captureOnCommitCallbacks(execute=True):
            _record(ticket=ticket, verdict="merge_safe")

        assert not ReviewLoop.objects.exists()
