import io
import json
from dataclasses import replace

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption
from teatree.core.models import Session, Task
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from tests._owner_channel import owner_card
from tests.factories import planned_ticket


class TestDelegatedDecision(TestCase):
    def _question(
        self, decision: OwnerDecision = OwnerDecision.PRODUCT_SCOPE, effect: str = "bounded"
    ) -> DeferredQuestion:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="shipping")
        card = replace(
            owner_card(decision),
            options=(
                CardOption("Proceed", "I run this step once.", recommended=True, effect=effect),
                CardOption("Skip", "I leave this step parked.", effect="bounded"),
            ),
        )
        return DeferredQuestion.record("Should this step run?", parked_task=task, card=card)

    def test_bounded_answer_is_audited_and_reaches_waiting_task(self) -> None:
        row = self._question()

        call_command(
            "questions",
            "delegate",
            row.pk,
            "Proceed",
            resolver_id="agent:session-1",
            evidence="The plan permits one run.",
        )

        row.refresh_from_db()
        assert row.resolved_via == DeferredQuestion.ResolvedVia.DELEGATED
        assert row.applied_at is not None
        audit = DeferredQuestionAudit.objects.get(question=row, action="delegated_answer")
        assert audit.resolver_id == "agent:session-1"
        assert "Rule -1" in audit.note
        assert row.parked_task.child_tasks.count() == 1
        assert "Proceed" in row.parked_task.child_tasks.get().execution_reason

        output = io.StringIO()
        call_command("questions", "list", "--all", "--json", stdout=output)
        listed = next(item for item in json.loads(output.getvalue()) if item["id"] == row.pk)
        assert listed["resolved_by"] == "agent:session-1"
        assert listed["resolution_reason"] == "Rule -1; evidence: The plan permits one run."
        assert listed["resolved_at"] is not None
        assert listed["applied_at"] is not None

    def test_hard_limit_or_standing_choice_is_refused(self) -> None:
        owner_only = "the card's decision class may include an owner-only effect"
        not_bounded = "the selected effect is not a bounded, delegated action"
        cases = (
            (OwnerDecision.MONEY_OR_PLAN, "spend", owner_only),
            (OwnerDecision.IRREVERSIBLE, "delete_unpushed", owner_only),
            (OwnerDecision.CREDENTIALS, "widen_security", owner_only),
            (OwnerDecision.PUBLIC_POST, "publish_as_owner", owner_only),
            (OwnerDecision.PRODUCT_SCOPE, "standing", not_bounded),
            (OwnerDecision.MONEY_OR_PLAN, "bounded", owner_only),
            (OwnerDecision.IRREVERSIBLE, "bounded", owner_only),
            (OwnerDecision.CREDENTIALS, "bounded", owner_only),
            (OwnerDecision.PUBLIC_POST, "bounded", owner_only),
        )
        for decision, effect, reason in cases:
            with self.subTest(decision=decision, effect=effect):
                row = self._question(decision, effect)
                stderr = io.StringIO()

                with pytest.raises(SystemExit) as refused:
                    call_command(
                        "questions",
                        "delegate",
                        row.pk,
                        "Proceed",
                        resolver_id="agent:session-1",
                        evidence="The prior decision was checked.",
                        stderr=stderr,
                    )

                assert refused.value.code == 2
                assert reason in stderr.getvalue()
                skipped = DeferredQuestionAudit.objects.get(question=row, action="delegation_skipped")
                assert skipped.note == f"Rule -1: {reason}"
                row.refresh_from_db()
                assert row.is_pending
                assert row.applied_at is None
                assert row.parked_task.child_tasks.count() == 0

    def test_answer_replay_does_not_duplicate_the_effect(self) -> None:
        row = self._question()
        args = ("questions", "delegate", row.pk, "Proceed")
        options = {"resolver_id": "agent:session-1", "evidence": "The plan permits one run."}

        call_command(*args, **options)
        with pytest.raises(SystemExit):
            call_command(*args, **options, stderr=io.StringIO())

        assert row.parked_task.child_tasks.count() == 1
