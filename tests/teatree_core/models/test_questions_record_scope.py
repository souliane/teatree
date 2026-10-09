"""``t3 questions record`` can record the same SHAPE the scanners do.

An agent recording a question from a turn names WHICH signal it records
(``--dedupe-marker``) and, only for a decision the owner alone makes, WHICH decision
(``--decision``, #5096). Without a decision the row is internal and never DM'd.
"""

import json

import pytest
from django.core.management import call_command

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.question_text import options_digest

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

    def test_a_decision_records_an_owner_row(self) -> None:
        call_command("questions", "record", "Rotate the deploy token?", "--decision", "credentials")

        [row] = list(DeferredQuestion.pending())
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_an_answered_owner_question_is_not_recorded_again(self) -> None:
        call_command("questions", "record", "Rotate the deploy token?", "--decision", "credentials")
        [first] = list(DeferredQuestion.pending())
        DeferredQuestion.consume(first.pk, answer="yes")

        call_command("questions", "record", "Rotate the deploy token?", "--decision", "credentials")

        assert DeferredQuestion.objects.count() == 1

    def test_options_carry_the_hash_a_digit_reply_is_checked_against(self) -> None:
        options = [{"label": "staging"}, {"label": "prod"}]
        call_command(
            "questions", "record", "Which env?", "--decision", "product_scope", "--options", json.dumps(options)
        )

        [row] = list(DeferredQuestion.pending())
        assert row.options_hash == options_digest(options)

    def test_an_unknown_decision_is_refused_rather_than_stored(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("questions", "record", "Which region?", "--decision", "everyone")

        assert exc.value.code == 2
        assert not list(DeferredQuestion.pending())
