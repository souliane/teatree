"""Migration 0125 refreshes every shipped description on a live database."""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from teatree.core.models import Loop
from teatree.loops.seed import DEFAULT_LOOPS, seed_default_loops_and_prompts

_BEFORE = ("core", "0124_delete_rows_of_retired_settings")
_AFTER = ("core", "0127_remove_session_repo_ledgers")


@pytest.mark.timeout(240)
class TestRefreshSeededLoopDescriptions(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def test_upgraded_seed_descriptions_match_a_fresh_seed(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        seed_default_loops_and_prompts()
        historical_loop = executor.loader.project_state([_BEFORE]).apps.get_model("core", "Loop")
        names = {spec.name for spec in DEFAULT_LOOPS}
        assert set(historical_loop.objects.values_list("name", flat=True)) >= names
        historical_loop.objects.filter(name__in=names).update(description="stale installed description")
        historical_loop.objects.create(
            name="operator_loop",
            script="src/teatree/loops/operator_loop/loop.py",
            delay_seconds=60,
            description="Operator description",
        )

        MigrationExecutor(connection).migrate([_AFTER])
        migrated = set(Loop.objects.filter(name__in=names).values_list("name", "description"))
        assert migrated == {(spec.name, spec.description) for spec in DEFAULT_LOOPS}
        assert Loop.objects.get(name="operator_loop").description == "Operator description"

        Loop.objects.filter(name__in=names).delete()
        seed_default_loops_and_prompts()
        fresh = set(Loop.objects.filter(name__in=names).values_list("name", "description"))
        assert migrated == fresh

    def test_shipped_cadences_refresh_with_text_but_operator_cadence_keeps_its_text(self) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        seed_default_loops_and_prompts()
        historical = executor.loader.project_state([_BEFORE]).apps.get_model("core", "Loop")
        older = {
            "triage_assessor": 3600,
            "arch_review": 10800,
            "eval_local": 86400,
            "db_backup": 86400,
        }
        for name, delay in older.items():
            historical.objects.filter(name=name).update(delay_seconds=delay, daily_at=None, description="old text")
        historical.objects.filter(name="issue_implementer").update(delay_seconds=300, description="operator text")
        historical.objects.filter(name="dogfood").update(description="old text")

        MigrationExecutor(connection).migrate([_AFTER])

        specs = {spec.name: spec for spec in DEFAULT_LOOPS}
        for name in older:
            loop = Loop.objects.get(name=name)
            assert (loop.delay_seconds, loop.daily_at, loop.description) == (
                specs[name].delay_seconds,
                specs[name].daily_at,
                specs[name].description,
            )
        tuned = Loop.objects.get(name="issue_implementer")
        assert (tuned.delay_seconds, tuned.daily_at, tuned.description) == (300, None, "operator text")
        current = Loop.objects.get(name="dogfood")
        assert (current.delay_seconds, current.daily_at, current.description) == (
            specs["dogfood"].delay_seconds,
            specs["dogfood"].daily_at,
            specs["dogfood"].description,
        )
