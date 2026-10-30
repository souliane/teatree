"""An owner asked to approve a rule sees the proposed words on the card."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import DeferredQuestion
from teatree.core.owner_question_message import render_text


class TestRulesFileCard(TestCase):
    def _record(self, *extra: str) -> tuple[int, str]:
        stderr = StringIO()
        try:
            call_command(
                "questions",
                "record",
                "Should I add this rule to the rules file?",
                "--decision",
                "architecture",
                "--checked",
                "The current rule does not cover this case.",
                "--why",
                "This changes how the factory handles later tasks.",
                "--blocker",
                "The owner must choose the rule wording.",
                *extra,
                stderr=stderr,
            )
        except SystemExit as exc:
            return int(exc.code or 0), stderr.getvalue()
        return 0, stderr.getvalue()

    def test_missing_proposed_text_is_refused(self) -> None:
        code, error = self._record()

        assert code == 2
        assert "proposed text" in error
        assert not DeferredQuestion.objects.exists()

    def test_exact_proposed_text_is_on_the_card(self) -> None:
        proposed = "An agent answers a settled question before raising it."

        code, error = self._record("--proposed-text", proposed)

        assert code == 0, error
        row = DeferredQuestion.objects.get()
        assert row.evidence["quoted"] == proposed
        assert proposed in render_text(row)
