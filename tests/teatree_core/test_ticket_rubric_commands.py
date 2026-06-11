"""`t3 ticket rubric-set | rubric-grade` — the rubric population + grading seams (#2241).

``call_command`` against real ``Ticket`` / ``Rubric`` rows. The set seam takes
EXPLICIT criteria (no ``/plan`` derivation); the grade seam records a verifier's
per-criterion PASS/FAIL through the guarded factory (grader != maker, SHA-bound).
"""

import json
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import RubricCriterion, Ticket

_SHA = "a" * 40


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)


class TestRubricSetCommand(TestCase):
    def test_set_creates_pending_criteria_from_string_array(self) -> None:
        ticket = _ticket()
        result = cast(
            "dict[str, object]",
            call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", json.dumps(["AC1", "AC2"])),
        )
        assert result["criteria_count"] == 2
        rubric = ticket.rubrics.get()
        assert [c.text for c in rubric.criteria.all()] == ["AC1", "AC2"]
        assert all(c.status == RubricCriterion.Status.PENDING for c in rubric.criteria.all())

    def test_set_accepts_text_objects(self) -> None:
        ticket = _ticket()
        call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", json.dumps([{"text": "AC1"}]))
        assert [c.text for c in ticket.rubrics.get().criteria.all()] == ["AC1"]

    def test_set_refuses_empty_array(self) -> None:
        ticket = _ticket()
        with pytest.raises(SystemExit):
            call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", "[]")

    def test_set_refuses_malformed_json(self) -> None:
        ticket = _ticket()
        with pytest.raises(SystemExit):
            call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", "{not json")

    def test_set_refuses_non_array(self) -> None:
        ticket = _ticket()
        with pytest.raises(SystemExit):
            call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", json.dumps({"text": "AC1"}))

    def test_set_with_no_input_returns_error(self) -> None:
        ticket = _ticket()
        result = cast("dict[str, object]", call_command("ticket", "rubric-set", str(ticket.pk)))
        assert "error" in result

    def test_set_refuses_non_string_non_object_item(self) -> None:
        ticket = _ticket()
        with pytest.raises(SystemExit):
            call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", json.dumps([123]))

    def test_set_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "rubric-set", "999999", "--criteria-json", json.dumps(["AC1"]))


class TestRubricGradeCommand(TestCase):
    def _ticket_with_rubric(self) -> Ticket:
        ticket = _ticket()
        call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", json.dumps(["AC1", "AC2"]))
        return ticket

    def test_grade_stamps_pass_and_reports_fully_passed(self) -> None:
        ticket = self._ticket_with_rubric()
        grades = json.dumps([{"ordinal": 0, "status": "pass"}, {"ordinal": 1, "status": "pass"}])
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                grades,
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            ),
        )
        assert result["graded_count"] == 2
        assert result["fully_passed"] is True

    def test_grade_partial_is_not_fully_passed(self) -> None:
        ticket = self._ticket_with_rubric()
        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps([{"ordinal": 0, "status": "pass"}]),
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            ),
        )
        assert result["graded_count"] == 1
        assert result["fully_passed"] is False

    def test_grade_refuses_maker_grader(self) -> None:
        ticket = self._ticket_with_rubric()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps([{"ordinal": 0, "status": "pass"}]),
                "--grader-identity",
                "merge-loop",
                "--reviewed-sha",
                _SHA,
            )

    def test_grade_refuses_truncated_sha(self) -> None:
        ticket = self._ticket_with_rubric()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps([{"ordinal": 0, "status": "pass"}]),
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                "abc123",
            )

    def test_grade_refuses_unknown_ordinal(self) -> None:
        ticket = self._ticket_with_rubric()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps([{"ordinal": 99, "status": "pass"}]),
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            )

    def test_grade_refuses_when_no_rubric(self) -> None:
        ticket = _ticket()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps([{"ordinal": 0, "status": "pass"}]),
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            )

    def test_grade_refuses_malformed_grades_json(self) -> None:
        ticket = self._ticket_with_rubric()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                "{not json",
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            )

    def test_grade_refuses_non_object_grade_item(self) -> None:
        ticket = self._ticket_with_rubric()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps(["not-an-object"]),
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            )

    def test_grade_refuses_grade_missing_status(self) -> None:
        ticket = self._ticket_with_rubric()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps([{"ordinal": 0}]),
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            )

    def test_grade_refuses_empty_grades(self) -> None:
        ticket = self._ticket_with_rubric()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                "[]",
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            )
