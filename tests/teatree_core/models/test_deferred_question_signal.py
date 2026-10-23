from dataclasses import replace
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption
from teatree.core.models.deferred_question import DeferredQuestion
from tests._owner_channel import answer_on_slack, owner_card


class TestResolvedSignalIdentity(TestCase):
    def test_settled_signal_does_not_expire_after_thirty_days(self) -> None:
        first = DeferredQuestion.objects.create(
            question="May this checkout be reused?",
            dedupe_marker="checkout:old-state",
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            answer_text="Yes",
            answered_at=timezone.now() - timedelta(days=40),
        )
        DeferredQuestion.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(days=40))

        repeated = DeferredQuestion.record(
            "May this checkout be reused?", dedupe_marker="checkout:old-state", card=owner_card()
        )

        assert repeated.pk == first.pk

    def test_dismissed_signal_replays_without_another_question(self) -> None:
        first = DeferredQuestion.record(
            "Should this bounded step run?", dedupe_marker="decision:ticket-1:step", card=owner_card()
        )
        first.mark_stale("The step is obsolete", resolver_id="test")

        repeated = DeferredQuestion.record(
            "Should this bounded step run?", dedupe_marker="decision:ticket-1:step", card=owner_card()
        )

        assert repeated.pk == first.pk
        assert repeated.dismissed_reason == "The step is obsolete"
        assert DeferredQuestion.objects.count() == 1

    def test_changed_named_fact_creates_a_new_signal(self) -> None:
        first = DeferredQuestion.record(
            "Should this bounded step run?", dedupe_marker="decision:ticket-1:step", card=owner_card()
        )
        answer_on_slack(first, "Yes")

        changed = DeferredQuestion.record(
            "Should this bounded step run?",
            dedupe_marker="decision:ticket-1:step",
            card=owner_card(OwnerDecision.PRODUCT_SCOPE, "The measured cost changed."),
        )

        assert changed.pk != first.pk
        assert changed.is_pending

    def test_changed_option_effect_creates_a_new_signal(self) -> None:
        card = owner_card()
        first = DeferredQuestion.record("Should this bounded step run?", dedupe_marker="step:bounded", card=card)
        answer_on_slack(first, "Yes")
        changed_card = replace(
            card,
            options=(
                CardOption("Yes", "I set a standing rule.", recommended=True, effect="standing"),
                CardOption("No", "I leave it as it is."),
            ),
        )

        changed = DeferredQuestion.record(
            "Should this bounded step run?", dedupe_marker="step:bounded", card=changed_card
        )

        assert changed.pk != first.pk
        assert changed.is_pending
