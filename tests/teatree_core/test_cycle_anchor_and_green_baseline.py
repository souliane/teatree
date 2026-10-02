"""A clamped cycle start and a blank SHA are both unrecoverable from their own value.

*   ``project_month_end_usd`` re-derived the billing anchor from the cycle's own
    start date. A cycle that began on a day a short month clamped (anchor 31 →
    Feb 28) then projected over a 28-day window, so the end-of-cycle figure came
    in low for every March.
*   ``record_run`` promoted every supplied per-repo SHA on green, including
    the ``""`` its caller writes when ``git rev-parse`` fails — wiping that
    repo's real baseline, so ``resolve_environment`` rebuilt an incomplete
    ``last_green`` after workspace cleanup.
"""

from datetime import date

import pytest
from django.test import TestCase

from teatree.core.cost import project_month_end_usd
from teatree.core.intake.e2e_workitem import E2ERecipe, RepoEntry, load_recipe, record_run, save_recipe
from teatree.core.models import Ticket


class TestProjectionKeepsTheConfiguredAnchor:
    def test_a_clamped_february_start_still_projects_to_the_anchor_day(self) -> None:
        projected = project_month_end_usd(
            2.0,
            cycle_start_date=date(2026, 2, 28),
            today=date(2026, 3, 1),
            anchor_day=31,
        )
        assert projected == pytest.approx(31.0)

    def test_without_an_anchor_the_start_day_is_still_used(self) -> None:
        projected = project_month_end_usd(
            2.0,
            cycle_start_date=date(2026, 2, 28),
            today=date(2026, 3, 1),
        )
        assert projected == pytest.approx(28.0)

    def test_a_calendar_month_cycle_is_unchanged(self) -> None:
        projected = project_month_end_usd(50.0, cycle_start_date=date(2026, 6, 1), today=date(2026, 6, 10))
        assert projected == pytest.approx(150.0)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestGreenBaselineNeverPromotesABlankSha(TestCase):
    def _ticket(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="t3-teatree", issue_url="https://example.com/issues/42")
        save_recipe(
            ticket,
            E2ERecipe(
                repos=[
                    RepoEntry(repo="repo-a", branch="main", last_green_sha="aaa"),
                    RepoEntry(repo="repo-b", branch="main", last_green_sha="bbb"),
                ]
            ),
        )
        return ticket

    def test_an_unresolved_head_leaves_the_prior_baseline_intact(self) -> None:
        ticket = self._ticket()
        record_run(ticket, result="green", per_repo_shas={"repo-a": "abc", "repo-b": ""})

        by_repo = {r.repo: r.last_green_sha for r in load_recipe(ticket).repos}
        assert by_repo == {"repo-a": "abc", "repo-b": "bbb"}

    def test_a_complete_green_run_still_promotes_every_sha(self) -> None:
        ticket = self._ticket()
        record_run(ticket, result="green", per_repo_shas={"repo-a": "abc", "repo-b": "def"})

        by_repo = {r.repo: r.last_green_sha for r in load_recipe(ticket).repos}
        assert by_repo == {"repo-a": "abc", "repo-b": "def"}
