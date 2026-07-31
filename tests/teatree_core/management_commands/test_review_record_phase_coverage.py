"""``review record --verdict merge_safe`` at the merge chokepoint (#3762).

The end-to-end shape of the historical failure: `t3 <overlay> review record …
--verdict merge_safe` is the one door every merge passes, and an out-of-band change walked
through it with a ticket whose lifecycle ledger carried nothing but
``reviewing``. These drive the real command, so the refusal, the structured
error result, and the two satisfiers are pinned at the surface the operator uses
— not only at the domain seam.
"""

from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ReviewVerdict, Session, Task, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.filterwarnings(
        "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning"
    ),
]

_SLUG = "souliane/teatree"
_PR_ID = 3710
_HEAD = "a" * 40


def _record(ticket: Ticket) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        call_command(
            "review",
            "record",
            str(_PR_ID),
            _SLUG,
            reviewed_sha=_HEAD,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
            ticket_id=ticket.pk,
        ),
    )


def _approve_out_of_band(ticket: Ticket, *, approver: str, reason: str) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        call_command(
            "lifecycle",
            "approve-out-of-band",
            str(ticket.pk),
            "--approver",
            approver,
            "--head-sha",
            _HEAD,
            "--reason",
            reason,
        ),
    )


def _ticket_338() -> Ticket:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
    session = Session.objects.create(ticket=ticket, overlay=ticket.overlay, visited_phases=["reviewing"])
    Task.objects.create(ticket=ticket, session=session, phase="reviewing", subject="review the PR")
    return ticket


class TestReviewRecordPhaseCoverage(TestCase):
    def test_refuses_and_records_nothing_for_the_338_shape(self) -> None:
        ticket = _ticket_338()
        result = _record(ticket)
        assert result["recorded"] is False
        assert "only at 'reviewing'" in str(result["error"])
        assert not ReviewVerdict.objects.for_pr(_SLUG, _PR_ID).exists()

    def test_recording_the_coding_phase_satisfies_it(self) -> None:
        ticket = _ticket_338()
        call_command("lifecycle", "visit-phase", str(ticket.pk), "coding")

        assert _record(ticket)["recorded"] is True
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR_ID).get().is_merge_safe()

    def test_a_human_out_of_band_override_satisfies_it(self) -> None:
        ticket = _ticket_338()
        _approve_out_of_band(ticket, approver="souliane", reason="dependency bump; no product code touched")

        assert _record(ticket)["recorded"] is True
        assert ReviewVerdict.objects.for_pr(_SLUG, _PR_ID).get().is_merge_safe()

    def test_an_agent_cannot_self_authorize_the_override(self) -> None:
        ticket = _ticket_338()
        result = _approve_out_of_band(ticket, approver="coding-agent", reason="I did the work myself")
        assert result["recorded"] is False
        assert _record(ticket)["recorded"] is False
