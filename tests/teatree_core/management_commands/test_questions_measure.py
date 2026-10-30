"""The owner-card report counts only cards that reached Slack."""

import datetime as dt
import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import DeferredQuestion
from tests._owner_channel import owner_card


class TestQuestionsMeasure(TestCase):
    def test_daily_kind_counts_exclude_unmirrored_and_old_cards(self) -> None:
        today = timezone.localdate().isoformat()
        for decision in (OwnerDecision.ARCHITECTURE, OwnerDecision.CREDENTIALS):
            row = DeferredQuestion.record(f"May I choose {decision.value}?", card=owner_card(decision))
            DeferredQuestion.objects.filter(pk=row.pk).update(slack_ts=f"1800000000.{row.pk:06d}")
        DeferredQuestion.record("Should we inspect an unmirrored card?", card=owner_card())
        old = DeferredQuestion.record("Should we inspect an old card?", card=owner_card())
        DeferredQuestion.objects.filter(pk=old.pk).update(
            slack_ts="1800000000.100000", created_at=timezone.now() - dt.timedelta(days=8)
        )
        DeferredQuestion.objects.create(
            question="Legacy scope?",
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            evidence={"decision": OwnerDecision.PRODUCT_SCOPE.value},
            slack_ts="1800000000.200000",
        )
        output = StringIO()

        call_command("questions", "measure", "--days", "7", "--json", stdout=output)

        rows = json.loads(output.getvalue())
        assert rows == [
            {"day": today, "kind": "architecture", "count": 1, "unexpected": False},
            {"day": today, "kind": "credentials", "count": 1, "unexpected": False},
            {"day": today, "kind": "product_scope", "count": 1, "unexpected": True},
        ]
