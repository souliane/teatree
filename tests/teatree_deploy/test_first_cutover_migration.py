"""The first roll off a legacy stack starts on the schema before the registry and drains before it migrates."""

import io
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.operations import CreateModel
from django.test import TransactionTestCase

from teatree.core.models import WorkerGeneration
from teatree.deploy.roll import RUNTIME_SERVICES, RollError, RollInterruptedError, RollOutcome
from teatree.generation import generation_image
from tests.teatree_deploy._fake_engine import FakeEngine, quiescing
from tests.teatree_deploy.test_roll import N1, _roller


def _schema_before_the_registry() -> tuple[str, str]:
    """The migration the registry's own migration depends on, read from the graph so a renumber cannot stale it."""
    migrations = MigrationExecutor(connection).loader.disk_migrations
    creates_registry = next(
        migration
        for (app, _name), migration in migrations.items()
        if app == "core"
        and any(isinstance(op, CreateModel) and op.name == "WorkerGeneration" for op in migration.operations)
    )
    return next(dependency for dependency in creates_registry.dependencies if dependency[0] == "core")


def _registry_table_exists() -> bool:
    return WorkerGeneration._meta.db_table in connection.introspection.table_names()


class _MigratingEngine(FakeEngine):
    def run_init(self, generation: str) -> None:
        self.calls.append(("run_init", generation))
        self.quiescing_at_init = quiescing()
        call_command("migrate", "core", "--no-input", verbosity=0)


class _InitFailsBeforeMigratingEngine(FakeEngine):
    def run_init(self, generation: str) -> None:
        self.calls.append(("run_init", generation))
        msg = "teatree-init exited 1"
        raise RollError(msg)


@pytest.mark.timeout(240)
class TestTheFirstRollOffALegacyStack(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)
        MigrationExecutor(connection).migrate([_schema_before_the_registry()])
        assert not _registry_table_exists()

    def test_it_drains_the_legacy_stack_before_its_init_migrates_then_registers(self) -> None:
        engine = _MigratingEngine(images={generation_image(N1): N1}, running=dict.fromkeys(RUNTIME_SERVICES, ""))
        steps: list[str] = []

        report = _roller(engine, steps).roll(N1)

        assert report.outcome is RollOutcome.ROLLED, report.detail
        assert steps.index("drain") < steps.index("init") < steps.index("register")
        assert engine.quiescing_at_init is True
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE

    def test_an_init_that_fails_before_creating_the_registry_restores_the_legacy_stack(self) -> None:
        engine = _InitFailsBeforeMigratingEngine(
            images={generation_image(N1): N1}, running=dict.fromkeys(RUNTIME_SERVICES, "")
        )

        report = _roller(engine).roll(N1)

        assert report.outcome is RollOutcome.ROLLED_BACK
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, "")
        assert not quiescing()
        assert not _registry_table_exists()

    def test_an_interrupted_first_cutover_reports_the_restored_legacy_stack(self) -> None:
        engine = FakeEngine(images={generation_image(N1): N1}, running=dict.fromkeys(RUNTIME_SERVICES, ""))
        engine.interrupt_init = RollInterruptedError
        stderr = io.StringIO()

        with (
            patch("teatree.deploy.compose_engine.DockerComposeEngine.from_environment", return_value=engine),
            pytest.raises(SystemExit) as exited,
        ):
            call_command("deploy_roll", to=N1, stable_seconds=0, stderr=stderr)

        assert exited.value.code == 3
        assert "the legacy stack is serving" in stderr.getvalue()
        assert engine.running == dict.fromkeys(RUNTIME_SERVICES, "")

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)
