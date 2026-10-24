import datetime as dt
import io
import json
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption
from teatree.core.models import BotPing, Session, Task
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
    resurface_question_backlog,
)
from tests._owner_channel import OWNER_DECISION, owner_card
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


class TestKeptCardIsReportedOnce(TestCase):
    def _kept_hard_limit_card(self) -> DeferredQuestion:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="shipping")
        card = replace(
            owner_card(OwnerDecision.MONEY_OR_PLAN),
            options=(CardOption("Spend", "I buy it.", recommended=True, effect="spend"), CardOption("Skip", "I wait.")),
        )
        kept = DeferredQuestion.record("Should this step spend money?", parked_task=task, card=card)
        with pytest.raises(SystemExit):
            call_command(
                "questions",
                "delegate",
                kept.pk,
                "Spend",
                resolver_id="agent:session-1",
                evidence="The decision class is owner-only.",
                stderr=io.StringIO(),
            )
        return kept

    def _mirror_and_age(self, *rows: DeferredQuestion) -> None:
        for index, row in enumerate(rows):
            DeferredQuestion.objects.filter(pk=row.pk).update(
                slack_channel="D-USER", slack_ts=f"10{index}.0", created_at=timezone.now() - dt.timedelta(days=3)
            )

    def _backend(self) -> MagicMock:
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.post_message.return_value = {"ok": True, "ts": "1800000000.000000"}
        backend.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1800000000000000"
        return backend

    def _posted(self, backend: MagicMock, text: str) -> bool:
        return any(text in str(call) for call in backend.post_message.call_args_list)

    def test_a_kept_card_is_not_pinged_again_by_any_repeat_drain(self) -> None:
        kept = self._kept_hard_limit_card()
        control = DeferredQuestion.record("Which DB host?", **OWNER_DECISION)
        self._mirror_and_age(kept, control)
        kept.refresh_from_db()
        control.refresh_from_db()
        backend = self._backend()

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify_question_drains.notify_user", return_value=True) as digest,
        ):
            assert resurface_question_backlog(user_id="U_ME", backend=backend) == (True, 1)
            assert reask_escalated_questions(user_id="U_ME", backend=backend) == (1, 1)
            assert drain_deferred_questions(user_id="U_ME", backend=backend) == (1, 1)

        threads = {call.kwargs.get("thread_ts") for call in backend.post_message.call_args_list}
        assert control.slack_ts in threads, "the control card was never pinged, so the probe proves nothing"
        assert kept.slack_ts not in threads, "a kept card was pinged again"
        assert "spend money" not in str(digest.call_args_list)
        kept.refresh_from_db()
        assert not kept.is_pending
        assert kept.dismissed_reason.startswith("Rule -1 skipped")

    def test_a_hard_limit_is_reported_as_skipped_without_posting_a_question(self) -> None:
        kept = self._kept_hard_limit_card()
        backend = self._backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            assert drain_unmirrored_deferred_questions(user_id="U_ME", backend=backend) == (0, 0)

        report = BotPing.objects.get(idempotency_key=f"rule1-hard-limit:{kept.stable_notify_ref}")
        assert "Rule -1 skipped" in report.text
        assert not self._posted(backend, "Should this step spend money?")
        kept.refresh_from_db()
        assert kept.slack_ts == ""
        assert kept.dismissed_reason.startswith("Rule -1 skipped")
        assert not kept.is_pending
