from importlib import import_module

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion


class TestRetireOperationalQuestions(TestCase):
    def test_only_pending_retired_card_classes_are_dismissed(self) -> None:
        retired = [
            DeferredQuestion.objects.create(question="Old card", dedupe_marker=marker)
            for marker in ("owner-wait:held-pr:1", "review-unrecordable:2", "stuck-redispatch-halt:3")
        ]
        unrelated = DeferredQuestion.objects.create(question="Current decision", dedupe_marker="owner-wait:public:4")
        answered = DeferredQuestion.objects.create(
            question="Already answered", dedupe_marker="stuck-redispatch-halt:5", answered_at=timezone.now()
        )
        retire = import_module("teatree.core.migrations.0027_retire_operational_questions").retire_operational_cards

        retire(apps, None)
        for question in retired:
            question.refresh_from_db()
            assert question.dismissed_at is not None
            assert question.dismissed_reason == "Automated workflow now resolves this state"
        unrelated.refresh_from_db()
        answered.refresh_from_db()
        assert unrelated.dismissed_at is None
        assert answered.dismissed_at is None

        first = [question.dismissed_at for question in retired]
        retire(apps, None)
        for question, dismissed_at in zip(retired, first, strict=True):
            question.refresh_from_db()
            assert question.dismissed_at == dismissed_at
