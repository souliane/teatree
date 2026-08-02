"""``t3 questions record`` can record the same SHAPE the scanners do.

An agent recording a question from a turn had no way to say WHICH signal it was
recording (``--dedupe-marker``) or WHO it was for (``--audience``), so its row
could not collapse onto the scanner's row for the same underlying condition and
an internal self-report recorded by hand still reached the owner's DM. Both are
columns the model already carries; only the command surface was missing.
"""

import pytest
from django.core.management import call_command

from teatree.core.models.deferred_question import DeferredQuestion

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


class TestRecordAudience:
    def test_internal_audience_is_recorded(self) -> None:
        call_command("questions", "record", "This session lacks a shell.", "--audience", "internal")

        [row] = list(DeferredQuestion.pending())
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_the_default_audience_is_the_owner(self) -> None:
        call_command("questions", "record", "Which region?")

        [row] = list(DeferredQuestion.pending())
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_an_unknown_audience_is_refused_rather_than_stored(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("questions", "record", "Which region?", "--audience", "everyone")

        assert exc.value.code == 2
        assert not list(DeferredQuestion.pending())
