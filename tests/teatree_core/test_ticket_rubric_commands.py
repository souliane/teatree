"""`t3 ticket rubric-set | rubric-grade` — the rubric population + grading seams (#2241).

``call_command`` against real ``Ticket`` / ``Rubric`` rows. The set seam takes
EXPLICIT criteria (no ``/plan`` derivation); the grade seam records a verifier's
per-criterion PASS/FAIL through the guarded factory (grader != maker, SHA-bound).
"""

import io
import json
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands._rubric_commands import RubricCommandError, parse_grades
from teatree.core.models import RubricCriterion, RubricError, Ticket

_SHA = "a" * 40
_CITATION = "unit: tests/teatree_core/test_ticket_rubric_commands.py"


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)


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

    def test_set_with_no_input_exits_nonzero(self) -> None:
        ticket = _ticket()
        with pytest.raises(SystemExit) as exc_info:
            call_command("ticket", "rubric-set", str(ticket.pk))
        assert exc_info.value.code == 1

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
        grades = json.dumps(
            [
                {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                {"ordinal": 1, "status": "pass", "rationale": _CITATION},
            ]
        )
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
                json.dumps([{"ordinal": 0, "status": "pass", "rationale": _CITATION}]),
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

    def test_a_refused_batch_stamps_nothing(self) -> None:
        # The atomicity the CLI inherits from `Rubric.apply_grades`: criterion 0's
        # grade is valid, criterion 1's PASS is uncited. A non-atomic run leaves the
        # rubric half-graded — a state no verifier chose to record.
        ticket = self._ticket_with_rubric()
        with pytest.raises(SystemExit):
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps(
                    [{"ordinal": 0, "status": "pass", "rationale": _CITATION}, {"ordinal": 1, "status": "pass"}]
                ),
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
            )

        assert [c.status for c in ticket.rubrics.get().criteria.all()] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]


class TestRubricShowCommand(TestCase):
    """`t3 ticket rubric-show` — the read seam a reviewer needs to see what it must grade (#4832)."""

    def _ticket_with_rubric(self) -> Ticket:
        ticket = _ticket()
        call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", json.dumps(["AC1", "AC2"]))
        return ticket

    def test_show_lists_every_criterion_pending(self) -> None:
        ticket = self._ticket_with_rubric()
        result = cast("dict[str, object]", call_command("ticket", "rubric-show", str(ticket.pk)))
        criteria = cast("list[dict[str, object]]", result["criteria"])
        assert [c["text"] for c in criteria] == ["AC1", "AC2"]
        assert [c["ordinal"] for c in criteria] == [0, 1]
        assert all(c["status"] == RubricCriterion.Status.PENDING for c in criteria)
        assert all(c["reviewed_sha"] == "" for c in criteria)

    def test_show_reflects_a_recorded_grade(self) -> None:
        ticket = self._ticket_with_rubric()
        call_command(
            "ticket",
            "rubric-grade",
            str(ticket.pk),
            "--grades-json",
            json.dumps([{"ordinal": 0, "status": "pass", "rationale": _CITATION}]),
            "--grader-identity",
            "cold-reviewer",
            "--reviewed-sha",
            _SHA,
        )
        result = cast("dict[str, object]", call_command("ticket", "rubric-show", str(ticket.pk)))
        criteria = cast("list[dict[str, object]]", result["criteria"])
        graded, pending = criteria[0], criteria[1]
        assert graded["status"] == RubricCriterion.Status.PASS
        assert graded["reviewed_sha"] == _SHA
        assert graded["grader_identity"] == "cold-reviewer"
        assert graded["rationale"] == _CITATION
        assert pending["status"] == RubricCriterion.Status.PENDING

    def test_show_refuses_when_no_rubric(self) -> None:
        ticket = _ticket()
        with pytest.raises(SystemExit):
            call_command("ticket", "rubric-show", str(ticket.pk))

    def test_show_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "rubric-show", "999999")


class TestParseGrades(TestCase):
    """The CLI parser yields the same :class:`RubricGrade` shape the envelope carries.

    The per-item refusals below are :meth:`Rubric.normalize_grades`, shared verbatim with
    the reviewing recorder — the CLI's own contract is now only the JSON and the
    non-empty-array rule, which the envelope path does not share.
    """

    def test_an_ordinal_string_is_coerced_to_the_int_the_lookup_needs(self) -> None:
        assert parse_grades(json.dumps([{"ordinal": "0", "status": "pass"}])) == [
            {"ordinal": 0, "status": "pass", "rationale": ""}
        ]

    def test_a_non_integer_ordinal_is_refused_by_name(self) -> None:
        with pytest.raises(RubricError, match="a grade ordinal must be an integer naming a criterion"):
            parse_grades(json.dumps([{"ordinal": "first", "status": "pass"}]))

    def test_a_float_ordinal_is_refused_rather_than_truncated(self) -> None:
        with pytest.raises(RubricError, match="a grade ordinal must be an integer naming a criterion"):
            parse_grades(json.dumps([{"ordinal": 1.5, "status": "pass"}]))

    def test_the_non_empty_array_rule_stays_the_command_own_refusal(self) -> None:
        # The envelope path does not share it — an absent ``rubric_grades`` grades nothing
        # there rather than refusing — so this rung stays behind the CLI's own error type.
        with pytest.raises(RubricCommandError, match="non-empty array"):
            parse_grades(json.dumps({"ordinal": 0, "status": "pass"}))

    def test_a_bare_string_item_is_refused_by_name(self) -> None:
        with pytest.raises(RubricError, match="must be an object, not a str"):
            parse_grades(json.dumps(["pass"]))

    def test_an_item_naming_the_wrong_status_field_is_refused_by_name(self) -> None:
        with pytest.raises(RubricError, match="needs an ordinal and a status"):
            parse_grades(json.dumps([{"ordinal": 0, "grade": "pass"}]))

    def test_a_malformed_item_names_the_refusal_on_stderr_and_exits_nonzero(self) -> None:
        ticket = _ticket()
        call_command("ticket", "rubric-set", str(ticket.pk), "--criteria-json", json.dumps(["the fix is pinned RED"]))
        stderr = io.StringIO()

        with pytest.raises(SystemExit) as raised:
            call_command(
                "ticket",
                "rubric-grade",
                str(ticket.pk),
                "--grades-json",
                json.dumps(["pass"]),
                "--grader-identity",
                "cold-reviewer",
                "--reviewed-sha",
                _SHA,
                stderr=stderr,
            )

        assert raised.value.code == 1
        assert "rubric-grade refused: each grade must be an object, not a str: 'pass'" in stderr.getvalue()
        assert [c.status for c in ticket.rubrics.get().criteria.all()] == [RubricCriterion.Status.PENDING]
