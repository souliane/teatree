"""``t3 questions record`` can record the same SHAPE the scanners do.

An agent recording a question from a turn names WHICH signal it records
(``--dedupe-marker``) and, only for a decision the owner alone makes, WHICH decision
(``--decision``, #5096). Without a decision the row is internal and never DM'd.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.modelkit.owner_decision import OWNER_QUESTION_ROUTE
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.question_text import options_digest

_CHECKED = ("--checked", "the runbook names no rotation owner")


def _record(*args: str) -> str:
    out = StringIO()
    call_command("questions", "record", *args, stdout=out)
    return out.getvalue()


# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestRecordDedupeMarker:
    def test_two_records_under_one_marker_collapse_to_a_single_pending_row(self) -> None:
        call_command("questions", "record", "Is MR 41 ready?", "--dedupe-marker", "mr-state:mr-41")
        call_command("questions", "record", "Is MR 41 ready, really?", "--dedupe-marker", "mr-state:mr-41")

        rows = list(DeferredQuestion.pending())
        assert len(rows) == 1
        assert rows[0].dedupe_marker == "mr-state:mr-41"
        assert rows[0].question == "Is MR 41 ready?"

    def test_distinct_markers_stay_distinct(self) -> None:
        """The control: a command that dropped the marker would also pass a same-marker test."""
        call_command("questions", "record", "Is MR 41 ready?", "--dedupe-marker", "mr-state:mr-41")
        call_command("questions", "record", "Is MR 42 ready?", "--dedupe-marker", "mr-state:mr-42")

        assert len(list(DeferredQuestion.pending())) == 2

    def test_no_marker_records_every_question_as_before(self) -> None:
        call_command("questions", "record", "Which region?")
        call_command("questions", "record", "Which region?")

        rows = list(DeferredQuestion.pending())
        assert len(rows) == 2
        assert {row.dedupe_marker for row in rows} == {""}


class TestRecordDecision:
    def test_no_decision_records_an_internal_row(self) -> None:
        call_command("questions", "record", "This session lacks a shell.")

        [row] = list(DeferredQuestion.pending())
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_an_internal_record_says_it_is_not_sent_and_names_the_route(self) -> None:
        out = _record("This session lacks a shell.")

        [row] = list(DeferredQuestion.pending())
        assert f"recorded #{row.pk} internal, not sent to the owner; decide it yourself" in out
        assert OWNER_QUESTION_ROUTE in out

    def test_a_decision_records_an_owner_row(self) -> None:
        call_command("questions", "record", "Rotate the deploy token?", "--decision", "credentials", *_CHECKED)

        [row] = list(DeferredQuestion.pending())
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_every_checked_fact_is_stored_as_evidence(self) -> None:
        call_command(
            "questions",
            "record",
            "Rotate the deploy token?",
            "--decision",
            "credentials",
            "--checked",
            "the runbook names no rotation owner",
            "--checked",
            "the token expires in 3 days",
        )

        [row] = list(DeferredQuestion.pending())
        assert row.evidence == {
            "decision": "credentials",
            "checked": ["the runbook names no rotation owner", "the token expires in 3 days"],
        }

    def test_a_decision_without_checked_is_refused_naming_the_flag(self) -> None:
        err = StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("questions", "record", "Rotate the deploy token?", "--decision", "credentials", stderr=err)

        assert exc.value.code == 2
        assert "--checked" in err.getvalue()
        assert not DeferredQuestion.objects.exists()

    def test_an_answered_owner_question_is_not_recorded_again(self) -> None:
        call_command("questions", "record", "Rotate the deploy token?", "--decision", "credentials", *_CHECKED)
        [first] = list(DeferredQuestion.pending())
        DeferredQuestion.consume(first.pk, answer="yes")

        call_command("questions", "record", "Rotate the deploy token?", "--decision", "credentials", *_CHECKED)

        assert DeferredQuestion.objects.count() == 1

    def test_a_sticky_hit_prints_the_earlier_answer(self) -> None:
        _record("Rotate the deploy token?", "--decision", "credentials", *_CHECKED)
        [first] = list(DeferredQuestion.pending())
        DeferredQuestion.consume(first.pk, answer="rotate it on Monday")

        out = _record("Rotate the deploy token?", "--decision", "credentials", *_CHECKED)

        assert f"#{first.pk}" in out
        assert "rotate it on Monday" in out

    def test_a_sticky_hit_prints_the_dismissal_reason(self) -> None:
        _record("Rotate the deploy token?", "--decision", "credentials", *_CHECKED)
        [first] = list(DeferredQuestion.pending())
        DeferredQuestion.consume(first.pk, dismissed_reason="the token was retired")

        out = _record("Rotate the deploy token?", "--decision", "credentials", *_CHECKED)

        assert f"#{first.pk}" in out
        assert "the token was retired" in out

    def test_options_carry_the_hash_a_digit_reply_is_checked_against(self) -> None:
        options = [{"label": "staging"}, {"label": "prod"}]
        call_command(
            "questions",
            "record",
            "Which env?",
            "--decision",
            "product_scope",
            *_CHECKED,
            "--options",
            json.dumps(options),
        )

        [row] = list(DeferredQuestion.pending())
        assert row.options_hash == options_digest(options)

    def test_an_unknown_decision_is_refused_rather_than_stored(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("questions", "record", "Which region?", "--decision", "everyone", *_CHECKED)

        assert exc.value.code == 2
        assert not list(DeferredQuestion.pending())
