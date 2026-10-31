"""A merge is refused while an acceptance item of the linked issue has no rubric criterion or owner waiver.

The rubric is built from the plan, so a rubric that passes fully can still say less than the
issue. Only the forge boundary (the issue read) is stubbed; the rubric rows, the waiver and
both merge callers are real.
"""

import contextlib
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.gates.rubric_gate import RubricNotSatisfiedError, check_rubric_satisfied, check_rubric_verified
from teatree.core.merge.authorization import _assert_rubric_satisfied
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.ticket_gates import assert_ticket_scoped_gates
from teatree.core.models import Rubric, Ticket
from teatree.core.models.rubric_waiver import waive_criterion
from tests.factories import MergeClearFactory, PullRequestFactory, waive_rubric

_SHA = "a" * 40
_CITATION = "integration: tests/teatree_core/gates/test_rubric_issue_coverage.py"
_ISSUE_URL = "https://github.com/souliane/teatree/issues/4242"
_REGRESSION = "A regression test goes through the real merge path"
_REPORT = "A read-only report lists the settled tickets"
_NO_SETTING = "No new setting key"
_ISSUE_BODY = (
    f"## Acceptance\n- {_REGRESSION}\n- {_REPORT}\n\n## Request added (2026-10-31)\n\n## Acceptance\n- {_NO_SETTING}\n"
)
_ISSUE = {"state": "open", "body": _ISSUE_BODY}


class _Provider:
    def __init__(self, host: MagicMock) -> None:
        self.host = host

    def get_code_host_for_url(self, overlay: object, issue_url: str) -> MagicMock:
        _ = (overlay, issue_url)
        return self.host


@contextlib.contextmanager
def _issue_read(payload: dict[str, str] | Exception) -> Generator[MagicMock]:
    host = MagicMock()
    if isinstance(payload, Exception):
        host.get_issue.side_effect = payload
    else:
        host.get_issue.return_value = payload
    with (
        patch("teatree.core.issue_coverage.get_backend_provider", return_value=_Provider(host)),
        patch("teatree.core.issue_coverage.get_overlay_for_ticket", return_value=object()),
    ):
        yield host


def _graded_ticket(*criteria: str, state: str = Ticket.State.REVIEW_REQUESTED) -> Ticket:
    ticket = Ticket.objects.create(overlay="t3-teatree", issue_url=_ISSUE_URL, state=state)
    rubric = Rubric.populate(ticket, list(criteria))
    for criterion in rubric.criteria:
        criterion.record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA, rationale=_CITATION)
    return ticket


class TestMergeNeedsAnyIssueItemCovered(TestCase):
    def test_each_item_without_a_criterion_is_named_including_one_under_a_later_acceptance_heading(self) -> None:
        ticket = _graded_ticket(f"{_REGRESSION}, and it fails on main")

        with _issue_read(_ISSUE), pytest.raises(RubricNotSatisfiedError) as refusal:
            check_rubric_satisfied(ticket, _SHA, transition="merge")

        assert _REPORT in str(refusal.value)
        assert _NO_SETTING in str(refusal.value)
        assert _REGRESSION not in str(refusal.value)

    def test_the_merge_passes_once_every_item_has_a_criterion_or_an_owner_waiver(self) -> None:
        ticket = _graded_ticket(_REGRESSION, _REPORT, _NO_SETTING)
        stated = Rubric.objects.active_for_ticket(ticket).criteria.get(text=_NO_SETTING)
        waive_criterion(ticket, text_hash=stated.text_hash, waived_by="owner", reason="staged for the next change")

        with _issue_read(_ISSUE):
            check_rubric_satisfied(ticket, _SHA, transition="merge")

    def test_an_issue_the_forge_cannot_read_refuses_the_merge(self) -> None:
        ticket = _graded_ticket(_REGRESSION, _REPORT, _NO_SETTING)

        with _issue_read(RuntimeError("forge down")), pytest.raises(RubricNotSatisfiedError, match="cannot be read"):
            check_rubric_satisfied(ticket, _SHA, transition="merge")

    def test_a_closed_issue_has_no_items_left_to_cover(self) -> None:
        ticket = _graded_ticket(_REGRESSION)

        with _issue_read({**_ISSUE, "state": "closed"}):
            check_rubric_satisfied(ticket, _SHA, transition="merge")

    def test_the_audited_plan_bypass_waives_the_coverage(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", issue_url=_ISSUE_URL)
        waive_rubric(ticket)

        with _issue_read(_ISSUE) as host:
            check_rubric_satisfied(ticket, _SHA, transition="merge")

        host.get_issue.assert_not_called()

    def test_a_ticket_with_no_forge_issue_reads_nothing(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", issue_url="")
        Rubric.populate(ticket, [_REGRESSION]).criteria.get().record_grade(
            status="pass", grader_identity="cold-reviewer", reviewed_sha=_SHA, rationale=_CITATION
        )

        with _issue_read(_ISSUE) as host:
            check_rubric_satisfied(ticket, _SHA, transition="merge")

        host.get_issue.assert_not_called()

    def test_delivery_stays_database_only(self) -> None:
        ticket = _graded_ticket(_REGRESSION, state=Ticket.State.RETRO_RECORDED)

        with _issue_read(_ISSUE) as host:
            check_rubric_verified(ticket)

        host.get_issue.assert_not_called()


class TestBothMergeCallersRefuse(TestCase):
    def test_the_clear_taking_caller_refuses_an_uncovered_item(self) -> None:
        ticket = _graded_ticket(_REGRESSION)
        clear = MergeClearFactory(ticket=ticket)

        with _issue_read(_ISSUE), pytest.raises(MergePreconditionError, match=_REPORT):
            _assert_rubric_satisfied(clear, _SHA)

    def test_the_pr_identity_caller_refuses_an_uncovered_item(self) -> None:
        ticket = _graded_ticket(_REGRESSION)
        PullRequestFactory(ticket=ticket, repo="souliane/teatree", iid="4242")

        with (
            _issue_read(_ISSUE),
            patch("teatree.core.merge.ticket_gates.anti_vacuity_gate.check_anti_vacuity_attestation"),
            pytest.raises(MergePreconditionError, match=_REPORT),
        ):
            assert_ticket_scoped_gates(slug="souliane/teatree", pr_id=4242, head_sha=_SHA)
