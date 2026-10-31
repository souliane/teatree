"""`t3 ticket plan-amend` — append acceptance criteria to a recorded plan without disturbing the rest.

The earlier criteria keep their text byte for byte and their grade; only the new one is
added, once, against the live origin/main head.
"""

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import RubricCriterion, Ticket, Worktree
from teatree.core.models.plan_artifact import PlanArtifact
from tests.teatree_core.test_ticket_plan_reaffirm import _advance_remote, _clone, _git, _make_remote

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_EARLIER = ("legacy plain criterion", {"text": "the command refuses a closing keyword", "kind": "behaviour"})
_RENDERED_EARLIER = ["legacy plain criterion", "[behaviour] the command refuses a closing keyword"]
_NEW = "ensure-pr reports an unreadable issue"


def _adequacy() -> dict:
    return {
        "design": {"content": "implement the change"},
        "integration_seams": {"content": ["src/seam.py"]},
        "edge_cases": {"content": ["stale base"]},
        "test_strategy": {"content": "red-first"},
        "acceptance_criteria": {"content": list(_EARLIER)},
    }


class TestPlanAmend(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp = tmp_path

    def _ticket_with_graded_plan(self, *, advanced_path: str = "src/other.py", planned_base: str = "") -> Ticket:
        bare = _make_remote(self._tmp)
        clone = _clone(self._tmp, bare)
        old_base = planned_base or _git(clone, "rev-parse", "HEAD")
        _advance_remote(self._tmp, bare, path=advanced_path)
        self.live_head = _git(bare, "rev-parse", "main")
        ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.PLAN_RECORDED)
        Worktree.objects.create(ticket=ticket, repo_path=str(clone), branch="f", extra={"worktree_path": str(clone)})
        PlanArtifact.record(
            ticket=ticket, plan_text="prior plan", recorded_by="t3:planner", base_sha=old_base, adequacy=_adequacy()
        )
        graded = RubricCriterion.objects.get(rubric__ticket=ticket, ordinal=0)
        graded.record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha="c" * 40, rationale="proved")
        return ticket

    def _amend(self, ticket: Ticket) -> None:
        call_command("ticket", "plan-amend", str(ticket.pk), "--add-criterion", _NEW, "--kind", "behaviour")

    def _criteria(self, ticket: Ticket) -> list[RubricCriterion]:
        return list(RubricCriterion.objects.filter(rubric__ticket=ticket).order_by("ordinal"))

    def test_earlier_texts_stay_byte_identical_and_keep_their_grade(self) -> None:
        ticket = self._ticket_with_graded_plan()

        self._amend(ticket)

        criteria = self._criteria(ticket)
        assert [row.text for row in criteria] == [*_RENDERED_EARLIER, f"[behaviour] {_NEW}"]
        assert (criteria[0].status, criteria[0].rationale) == ("pass", "proved")

    def test_the_new_plan_is_bound_to_the_live_origin_main_head(self) -> None:
        ticket = self._ticket_with_graded_plan()

        self._amend(ticket)

        latest = PlanArtifact.objects.filter(ticket=ticket).order_by("-recorded_at", "-pk").first()
        assert latest is not None
        assert latest.base_sha == self.live_head

    def test_a_seam_change_since_the_plan_refuses_and_points_at_reaffirm(self) -> None:
        ticket = self._ticket_with_graded_plan(advanced_path="src/seam.py")
        stderr = StringIO()

        with pytest.raises(SystemExit):
            call_command(
                "ticket", "plan-amend", str(ticket.pk), "--add-criterion", _NEW, "--kind", "behaviour", stderr=stderr
            )

        assert "plan-reaffirm" in stderr.getvalue()
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 1

    def test_a_plan_base_the_checkout_cannot_resolve_refuses_rather_than_rebinding(self) -> None:
        ticket = self._ticket_with_graded_plan(planned_base="f" * 40)
        stderr = StringIO()

        with pytest.raises(SystemExit):
            call_command(
                "ticket", "plan-amend", str(ticket.pk), "--add-criterion", _NEW, "--kind", "behaviour", stderr=stderr
            )

        assert "plan-reaffirm" in stderr.getvalue()
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 1

    def test_a_second_identical_run_adds_nothing(self) -> None:
        ticket = self._ticket_with_graded_plan()
        self._amend(ticket)
        plans = PlanArtifact.objects.filter(ticket=ticket).count()
        criteria = [row.text for row in self._criteria(ticket)]

        self._amend(ticket)

        assert PlanArtifact.objects.filter(ticket=ticket).count() == plans
        assert [row.text for row in self._criteria(ticket)] == criteria

    def test_an_unknown_kind_is_refused_and_records_nothing(self) -> None:
        ticket = self._ticket_with_graded_plan()
        stderr = StringIO()

        with pytest.raises(SystemExit):
            call_command(
                "ticket", "plan-amend", str(ticket.pk), "--add-criterion", _NEW, "--kind", "vibes", stderr=stderr
            )

        assert "kind" in stderr.getvalue()
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 1

    def test_a_ticket_without_a_plan_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", state=Ticket.State.PLAN_RECORDED)
        stderr = StringIO()

        with pytest.raises(SystemExit):
            call_command(
                "ticket", "plan-amend", str(ticket.pk), "--add-criterion", _NEW, "--kind", "behaviour", stderr=stderr
            )

        assert "no plan to amend" in stderr.getvalue()
