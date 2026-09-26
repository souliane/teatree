"""``t3 <overlay> review record --rubric-grades-json`` — the operator rubric-stamping seam (#4832).

Stamps the PR's gated ticket's rubric in the SAME transaction as the verdict, through the
SAME coverage/contradiction validator the reviewing-phase envelope recorder runs
(``teatree.core.review.rubric_grading.validate_rubric_grades``) — so a human operator
transcribing an agent's grades can never accept a grading the agent's own path would refuse.
Omitting the flag is BYTE-IDENTICAL to before it existed.
"""

from io import StringIO
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import PullRequest, ReviewVerdict, Rubric, RubricCriterion, Ticket

_SLUG = "acme/widgets"
_PR_ID = 77
_HEAD = "497d468df76022b280caffceb400739d5ced9baa"
_CITATION = "unit: tests/teatree_core/management/commands/test_review_record_rubric_grades.py"


def _gated_ticket(*criteria: str) -> Ticket:
    ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR, state=Ticket.State.REVIEW_REQUESTED)
    PullRequest.objects.create(
        ticket=ticket, repo=_SLUG, iid=str(_PR_ID), url=f"https://github.com/{_SLUG}/pull/{_PR_ID}"
    )
    if criteria:
        Rubric.populate(ticket, list(criteria))
    return ticket


def _record(
    *, verdict: str = "hold", rubric_grades_json: str = "", stderr: StringIO | None = None
) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        call_command(
            "review",
            "record",
            str(_PR_ID),
            _SLUG,
            reviewed_sha=_HEAD,
            verdict=verdict,
            reviewer_identity="cold-reviewer-agent",
            gh_verify_result="green",
            blast_class="logic",
            rubric_grades_json=rubric_grades_json,
            stderr=stderr if stderr is not None else StringIO(),
        ),
    )


class TestOmittedFlagIsANoOp(TestCase):
    def test_no_flag_records_the_verdict_and_touches_no_rubric(self) -> None:
        ticket = _gated_ticket("AC1")
        result = _record()
        assert result["recorded"] is True
        assert "rubric_graded_count" not in result
        assert [c.status for c in Rubric.objects.active_for_ticket(ticket).criteria.all()] == [
            RubricCriterion.Status.PENDING
        ]


class TestFullCoverageIsStamped(TestCase):
    def test_grades_land_on_the_gated_ticket_in_the_same_call(self) -> None:
        ticket = _gated_ticket("AC1", "AC2")
        result = _record(
            rubric_grades_json=(
                f'[{{"ordinal": 0, "status": "pass", "rationale": "{_CITATION}"}}, '
                f'{{"ordinal": 1, "status": "pass", "rationale": "{_CITATION}"}}]'
            )
        )
        assert result["recorded"] is True
        assert result["rubric_graded_count"] == 2
        rubric = Rubric.objects.active_for_ticket(ticket)
        assert all(c.status == RubricCriterion.Status.PASS for c in rubric.criteria.all())
        assert all(c.grader_identity == "cold-reviewer-agent" for c in rubric.criteria.all())
        assert all(c.reviewed_sha == _HEAD for c in rubric.criteria.all())


class TestUngradedCoverageRefusesTheWholeRecord(TestCase):
    def test_an_ungraded_criterion_refuses_and_records_no_verdict_either(self) -> None:
        _gated_ticket("AC1", "AC2")
        stderr = StringIO()
        with pytest.raises(SystemExit):
            _record(
                rubric_grades_json='[{"ordinal": 0, "status": "pass", "rationale": "unit"}]',
                stderr=stderr,
            )
        assert "ungraded" in stderr.getvalue()
        assert ReviewVerdict.objects.count() == 0


class TestMergeSafeOverFailIsRefused(TestCase):
    def test_merge_safe_over_a_failed_criterion_refuses_before_any_write(self) -> None:
        _gated_ticket("AC1")
        stderr = StringIO()
        with pytest.raises(SystemExit):
            _record(
                verdict="merge_safe",
                rubric_grades_json='[{"ordinal": 0, "status": "fail"}]',
                stderr=stderr,
            )
        assert "merge_safe" in stderr.getvalue()
        assert ReviewVerdict.objects.count() == 0


class TestNoRubricToGrade(TestCase):
    def test_flag_given_but_gated_ticket_has_no_rubric_is_refused(self) -> None:
        _gated_ticket()  # no criteria populated — no Rubric row exists
        stderr = StringIO()
        with pytest.raises(SystemExit):
            _record(rubric_grades_json='[{"ordinal": 0, "status": "pass"}]', stderr=stderr)
        assert "no rubric to grade" in stderr.getvalue()
        assert ReviewVerdict.objects.count() == 0


class TestMalformedJson(TestCase):
    def test_invalid_json_is_refused_by_name(self) -> None:
        _gated_ticket("AC1")
        stderr = StringIO()
        with pytest.raises(SystemExit):
            _record(rubric_grades_json="{not json", stderr=stderr)
        assert "not valid JSON" in stderr.getvalue()
        assert ReviewVerdict.objects.count() == 0
