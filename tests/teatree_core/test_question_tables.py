"""The plain tables ``questions list`` and ``questions reachability`` print."""

from django.test import TestCase

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.question_tables import QuestionReachRow, render_questions_table, render_reachability_table


class TestQuestionTables(TestCase):
    def test_the_list_table_shows_each_question_and_the_count(self) -> None:
        row = DeferredQuestion.record("Which runner image?")

        text = render_questions_table([row])

        assert "1 open question(s)" in text
        assert "Which runner image?" in text

    def test_the_reachability_table_counts_rows_no_resolver_reaches(self) -> None:
        rows: list[QuestionReachRow] = [
            {"id": 1, "has_subject": True, "resolvers": {"subject_terminal": "keep"}},
            {"id": 2, "has_subject": False, "resolvers": {}},
        ]

        text = render_reachability_table(rows)

        assert "2 pending, 1 reachable by no resolver" in text
        assert "subject_terminal=keep" in text
