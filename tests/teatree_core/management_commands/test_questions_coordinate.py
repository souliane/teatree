"""An attended coordinator can cite an exact owner answer without minting a new grant."""

import dataclasses
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import DeferredQuestion
from tests._owner_channel import owner_card


class TestQuestionsCoordinate(TestCase):
    def _command(
        self, row: DeferredQuestion, basis: DeferredQuestion, text: str, *, dispatched: bool = False
    ) -> tuple[int, str]:
        stderr = StringIO()
        with patch("teatree.core.management.commands.questions.is_dispatched_agent_process", return_value=dispatched):
            try:
                call_command("questions", "coordinate", row.pk, "--basis", basis.pk, text, stderr=stderr)
            except SystemExit as exc:
                return int(exc.code or 0), stderr.getvalue()
        return 0, stderr.getvalue()

    def test_attended_command_uses_matching_owner_answer(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=owner_card())
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=owner_card())

        code, error = self._command(pending, basis, "Yes.")

        assert code == 0, error
        pending.refresh_from_db()
        assert pending.answer_text == "Yes."
        assert pending.audits.filter(action="coordinated", resolver_id="coordinator").exists()

    def test_hard_limit_and_local_basis_are_refused(self) -> None:
        hard = DeferredQuestion.record(
            "May I use the production credential?", card=owner_card(OwnerDecision.CREDENTIALS)
        )
        hard_basis = DeferredQuestion.record(hard.question, card=owner_card(OwnerDecision.CREDENTIALS))
        assert hard_basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        code, _ = self._command(hard, hard_basis, "Yes.")
        assert code == 2
        local_basis = DeferredQuestion.record("Should we adopt the new dependency?", card=owner_card())
        assert local_basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.LOCAL)
        pending = DeferredQuestion.record(local_basis.question, card=owner_card())
        code, _ = self._command(pending, local_basis, "Yes.")
        assert code == 2
        assert DeferredQuestion.objects.filter(pk__in=[hard.pk, pending.pk], answered_at__isnull=True).count() == 2

    def test_rules_file_card_and_changed_proposed_text_are_refused(self) -> None:
        rules_card = dataclasses.replace(owner_card(), quoted="Agents merge when every check is green.")
        rules_basis = DeferredQuestion.record("Should the rules file let agents merge?", card=rules_card)
        assert rules_basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        rules_pending = DeferredQuestion.record(rules_basis.question, card=rules_card)
        basis = DeferredQuestion.record(
            "Should we adopt the new dependency?", card=dataclasses.replace(owner_card(), quoted="Use the first one.")
        )
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        reworded = DeferredQuestion.record(
            basis.question, card=dataclasses.replace(owner_card(), quoted="Use the second one.")
        )

        assert self._command(rules_pending, rules_basis, "Yes.")[0] == 2
        assert self._command(reworded, basis, "Yes.")[0] == 2
        assert (
            DeferredQuestion.objects.filter(pk__in=[rules_pending.pk, reworded.pk], answered_at__isnull=True).count()
            == 2
        )

    def test_dispatched_agent_cannot_coordinate(self) -> None:
        basis = DeferredQuestion.record("Should we adopt the new dependency?", card=owner_card())
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(basis.question, card=owner_card())

        code, _ = self._command(pending, basis, "Yes.", dispatched=True)

        assert code == 2
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending
