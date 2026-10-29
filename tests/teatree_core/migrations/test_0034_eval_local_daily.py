from importlib import import_module

from django.apps import apps
from django.test import TestCase

from teatree.core.models import Loop


class TestEvalLocalDailyMigration(TestCase):
    def test_upgrades_only_the_shipped_weekly_cadence(self) -> None:
        loop, _ = Loop.objects.update_or_create(
            name="eval_local",
            defaults={
                "script": "src/teatree/loops/eval_local/loop.py",
                "delay_seconds": 604800,
                "description": "Runs the local behavioral eval suite weekly; this row IS the cadence.",
            },
        )
        migrate = import_module("teatree.core.migrations.0034_eval_local_daily").upgrade_eval_local_cadence

        migrate(apps, None)
        loop.refresh_from_db()
        assert loop.delay_seconds == 86400
        assert loop.description == "Runs one shard of behavioral evals daily on the factory subscription account."

        loop.delay_seconds = 604800
        loop.description = "operator description"
        loop.save(update_fields=["delay_seconds", "description"])
        migrate(apps, None)
        loop.refresh_from_db()
        assert loop.delay_seconds == 86400
        assert loop.description == "operator description"

        loop.delay_seconds = 172800
        loop.save(update_fields=["delay_seconds"])
        migrate(apps, None)
        loop.refresh_from_db()
        assert loop.delay_seconds == 172800
