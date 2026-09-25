"""The ``0093`` backfill grandfathers escalations accrued before the ladder was bounded (#4748).

Every count on a row at migration time was stamped under the pre-#4706 rule that an
escalation resolves nothing, so reading them as rungs dismissed a legacy row on the FIRST
sweep after deploy with no ask under the new semantics. Anti-vacuous: dropping the
``RunPython`` leaves ``escalation_base`` at 0 and the legacy row drains immediately.
"""

import importlib
from datetime import timedelta

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConfigSetting
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_drain import drain_pending_questions

# A migration module name starts with a digit, so it is unreachable by import syntax.
_migration = importlib.import_module("teatree.core.migrations.0093_deferred_question_escalation_base")
_grandfather = _migration.grandfather_pre_bound_escalations


def _legacy(*, count: int) -> DeferredQuestion:
    row = DeferredQuestion.record(f"A pre-bound question at {count}")
    DeferredQuestion.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(days=41),
        escalation_count=count,
        escalated_at=timezone.now() - timedelta(days=4),
    )
    row.refresh_from_db()
    return row


class TestGrandfatherPreBoundEscalations(TestCase):
    def test_an_escalated_row_starts_its_bounded_ladder_where_it_stands(self) -> None:
        row = _legacy(count=9)

        _grandfather(apps, None)

        row.refresh_from_db()
        assert (row.escalation_count, row.escalation_base) == (9, 9)
        assert row.bounded_escalations == 0

    def test_a_never_escalated_row_is_left_at_the_bottom(self) -> None:
        row = DeferredQuestion.record("Never escalated")

        _grandfather(apps, None)

        row.refresh_from_db()
        assert (row.escalation_count, row.escalation_base) == (0, 0)

    def test_the_grandfathered_row_is_asked_again_before_it_can_drain(self) -> None:
        ConfigSetting.objects.set_value("deferred_question_age_ceiling_days", 3)
        ConfigSetting.objects.set_value("deferred_question_max_escalations", 3)
        row = _legacy(count=9)

        _grandfather(apps, None)
        report = drain_pending_questions()

        assert (report.escalated, report.expired) == (1, 0)
        row.refresh_from_db()
        assert row.is_pending
