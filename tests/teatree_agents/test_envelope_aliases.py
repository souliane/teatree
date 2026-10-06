"""A reviewer's measured key drifts land on the schema's own names before recording.

Each drift below discarded a complete cold review in production: the recorder refused the
envelope, nothing recorded, and the pull request waited for a human.
"""

import json

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.envelope_aliases import normalize_envelope_aliases
from teatree.core.models import ReviewVerdict, RubricCriterion, Task
from teatree.core.models.ticket_evidence import recorded_anti_vacuity_attestation
from tests.teatree_agents.test_review_envelope_recorder import (
    _HEAD,
    _PR_ID,
    _SLUG,
    _author_ticket,
    _criteria,
    _envelope,
    _full_pass,
    _reviewing_task_via_dispatch,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestTheMeasuredDriftsRecord(TestCase):
    def test_a_work_item_url_review_context_records_its_verdict(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        result = _envelope(grades=_full_pass())
        context = result["review_context"]
        assert isinstance(context, dict)
        context["work_item_url"] = context.pop("work_item")

        attempt = record_result_envelope(task, result, phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert ReviewVerdict.objects.filter(ticket=ticket, slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()

    def test_top_level_rubric_grades_record_on_the_rubric(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        result = _envelope()
        result["rubric_grades"] = _full_pass()

        attempt = record_result_envelope(task, result, phase="reviewing")

        assert attempt.error == ""
        assert [c.status for c in _criteria(ticket)] == [RubricCriterion.Status.PASS, RubricCriterion.Status.PASS]

    def test_a_list_ac_coverage_records_as_its_json_text(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        coverage = [{"criterion": "the recorder stamps every grade", "tests": ["tests/a.py::test_b"]}]
        result = _envelope(grades=_full_pass())
        anti_vacuity = result["anti_vacuity"]
        assert isinstance(anti_vacuity, dict)
        anti_vacuity["ac_coverage"] = coverage

        attempt = record_result_envelope(task, result, phase="reviewing")

        assert attempt.error == ""
        ticket.refresh_from_db()
        assert json.loads(recorded_anti_vacuity_attestation(ticket, _HEAD)["ac_coverage"]) == coverage


class TestOnlyUnambiguousDriftIsRewritten:
    def test_work_item_wins_over_work_item_url(self) -> None:
        result = {"review_context": {"work_item": "issue", "work_item_url": "other"}}

        assert normalize_envelope_aliases(result) == result

    def test_grades_already_inside_the_verdict_are_not_overwritten(self) -> None:
        result = {"review_verdict": {"rubric_grades": [1]}, "rubric_grades": [2]}

        assert normalize_envelope_aliases(result) == result

    def test_invented_anti_vacuity_keys_are_never_mapped(self) -> None:
        result = {"anti_vacuity": {"ac_coverage": "mapped", "revert_fix_red": [{"test": "t", "result": "RED"}]}}

        assert normalize_envelope_aliases(result) == result

    def test_an_empty_ac_coverage_list_is_left_for_the_refusal(self) -> None:
        result = {"anti_vacuity": {"ac_coverage": []}}

        assert normalize_envelope_aliases(result) == result

    def test_the_callers_envelope_is_left_untouched(self) -> None:
        result = {"review_context": {"work_item_url": "issue"}, "review_verdict": {}, "rubric_grades": []}
        snapshot = json.loads(json.dumps(result))

        normalize_envelope_aliases(result)

        assert result == snapshot
