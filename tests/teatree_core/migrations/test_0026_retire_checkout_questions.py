from importlib import import_module

from django.apps import apps
from django.test import TestCase

from teatree.core.models import DeferredQuestion


class TestRetireCheckoutQuestions(TestCase):
    def test_only_pending_legacy_cards_are_dismissed_idempotently(self) -> None:
        legacy = DeferredQuestion.objects.create(
            question="Old checkout approval", evidence={"action": {"kind": "attest"}}
        )
        unrelated = DeferredQuestion.objects.create(question="Current decision")
        retire = import_module("teatree.core.migrations.0026_retire_checkout_questions").retire_checkout_cards

        retire(apps, None)
        legacy.refresh_from_db()
        unrelated.refresh_from_db()
        first = legacy.dismissed_at
        assert first is not None
        assert legacy.dismissed_reason == "Factory checkout safety now decides this state"
        assert unrelated.dismissed_at is None

        retire(apps, None)
        legacy.refresh_from_db()
        assert legacy.dismissed_at == first
