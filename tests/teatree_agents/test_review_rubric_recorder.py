from django.test import TestCase

from teatree.agents.review_rubric_recorder import ReviewGrading
from teatree.core.models import Rubric, RubricCriterion, Ticket


class TestReviewGrading(TestCase):
    def test_carries_prior_pass_without_replacing_an_override(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/1")
        rubric = Rubric.objects.create(ticket=ticket)
        carried = RubricCriterion.objects.create(rubric=rubric, ordinal=0, text="old path stays valid")
        overridden = RubricCriterion.objects.create(rubric=rubric, ordinal=1, text="new path must be checked")
        prior = "a" * 40
        head = "b" * 40
        for criterion in (carried, overridden):
            criterion.record_grade(
                status="pass", grader_identity="cold-reviewer-one", reviewed_sha=prior, rationale="tests/old.py:1"
            )

        ReviewGrading(
            rubric,
            [{"ordinal": 1, "status": "fail", "rationale": "new evidence"}],
            frozenset({0, 1}),
            prior,
        ).apply(identity="cold-reviewer-two", head=head)

        grades = Ticket.objects.get(pk=ticket.pk).extra["rubric_grades_by_head"][head]
        assert grades[str(carried.pk)]["grader_identity"] == "cold-reviewer-one"
        assert grades[str(overridden.pk)]["status"] == "fail"
        assert grades[str(overridden.pk)]["grader_identity"] == "cold-reviewer-two"
