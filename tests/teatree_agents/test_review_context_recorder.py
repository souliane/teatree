"""A refused review names the key the reviewer got wrong, and nothing it got right."""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.core.modelkit.task_failure_taxonomy import FailureKind, classify_failure
from teatree.core.models import ReviewVerdict, Session, Task, TaskAttempt, Ticket
from tests.teatree_agents.test_review_envelope_recorder import (
    _author_ticket,
    _envelope,
    _full_pass,
    _reviewing_task_via_dispatch,
)
from tests.teatree_core._self_review_helpers import author_ticket, self_review_result

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _refusal(*, block: str, value: dict[str, object]) -> str:
    _author_ticket()
    result = _envelope(grades=_full_pass())
    result[block] = value

    attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

    assert not ReviewVerdict.objects.exists()
    assert classify_failure(attempt.error) == FailureKind.RECORDING_REFUSED
    return attempt.error


class TestReviewContextRefusal(TestCase):
    def test_a_missing_work_item_is_named_alone(self) -> None:
        error = _refusal(block="review_context", value={"documents": ["spec.md"], "analysis": "checked"})

        assert error == "review context recording refused: missing work_item"

    def test_a_misnamed_key_is_named_beside_the_keys_the_block_takes(self) -> None:
        error = _refusal(
            block="review_context", value={"work_link": "issue", "documents": ["spec.md"], "analysis": "checked"}
        )

        assert "missing work_item" in error
        assert "unknown keys work_link" in error
        assert "work_item, documents, analysis" in error


class TestAntiVacuityRefusal(TestCase):
    def test_invented_keys_and_the_missing_proof_are_named(self) -> None:
        error = _refusal(
            block="anti_vacuity",
            value={"ac_coverage": "every AC maps to a test", "revert_fix_red": "reverted, went red", "gap": "none"},
        )

        assert error.startswith("anti-vacuity recording refused: ")
        assert "missing proven_tests or no_new_tests" in error
        assert "unknown keys gap, revert_fix_red" in error
        assert "ac_coverage, proven_tests, no_new_tests" in error

    def test_a_non_string_no_new_tests_is_named(self) -> None:
        error = _refusal(block="anti_vacuity", value={"ac_coverage": "mapped", "no_new_tests": "false"})

        assert "no_new_tests must be true or false" in error
        assert "unknown keys" not in error


class TestAHoldOwesNoAntiVacuity(TestCase):
    """Anti-vacuity is merge_safe evidence; a HOLD with its review context still lands its findings."""

    def _returned_without_anti_vacuity(self, verdict: str) -> TaskAttempt:
        ticket = author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="review")
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        task.claim(claimed_by="self-reviewer")
        result = self_review_result(verdict)
        del result["anti_vacuity"]
        with patch.object(Ticket, "has_shippable_diff", return_value=True):
            return record_result_envelope(task, result, phase="reviewing")

    def test_a_hold_completes_and_queues_its_rework(self) -> None:
        attempt = self._returned_without_anti_vacuity("hold")

        assert attempt.error == ""
        assert Task.objects.get(pk=attempt.task_id).status == Task.Status.COMPLETED
        assert Task.objects.filter(
            ticket_id=attempt.task.ticket_id, phase="coding", parent_task_id=attempt.task_id
        ).exists()

    def test_a_merge_safe_is_still_refused_for_the_missing_proof(self) -> None:
        attempt = self._returned_without_anti_vacuity("merge_safe")

        assert attempt.error.startswith("anti-vacuity recording refused: missing anti_vacuity")
        assert Task.objects.get(pk=attempt.task_id).status == Task.Status.FAILED
