# test-path: cross-cutting
import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from teatree.config import retired_settings

_BEFORE = ("core", "0098_a_loss_free_sweep_keeps_its_own_plan")
_AFTER = ("core", "0099_carry_the_venv_idle_window_onto_every_artifact")
_OLD_KEY = "venv_idle_days"
_NEW_KEY = "artifact_idle_days"


@pytest.mark.timeout(240)
class TestCarryVenvIdleWindow(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _seed_before(rows: tuple[tuple[str, str, str], ...]) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        config_setting = executor.loader.project_state(_BEFORE).apps.get_model("core", "ConfigSetting")
        config_setting.objects.all().delete()
        for scope, key, value in rows:
            config_setting.objects.create(scope=scope, key=key, value=value)

    @staticmethod
    def _migrate_and_read(target: tuple[str, str] = _AFTER) -> dict[tuple[str, str], str]:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([target])
        config_setting = executor.loader.project_state(target).apps.get_model("core", "ConfigSetting")
        return {(row.scope, row.key): row.value for row in config_setting.objects.all()}

    def test_global_and_overlay_values_move_independently(self) -> None:
        self._seed_before((("", _OLD_KEY, "2"), ("t3-teatree", _OLD_KEY, "9")))

        rows = self._migrate_and_read()

        assert rows == {("", _NEW_KEY): "2", ("t3-teatree", _NEW_KEY): "9"}

    def test_the_canonical_key_wins_per_scope(self) -> None:
        self._seed_before(
            (
                ("", _OLD_KEY, "2"),
                ("", _NEW_KEY, "7"),
                ("t3-teatree", _OLD_KEY, "9"),
            )
        )

        rows = self._migrate_and_read()

        assert rows == {("", _NEW_KEY): "7", ("t3-teatree", _NEW_KEY): "9"}

    def test_reverse_restores_the_old_key(self) -> None:
        self._seed_before((("", _OLD_KEY, "2"), ("t3-teatree", _OLD_KEY, "9")))
        self._migrate_and_read()

        rows = self._migrate_and_read(_BEFORE)

        assert rows == {("", _OLD_KEY): "2", ("t3-teatree", _OLD_KEY): "9"}

    def test_reverse_keeps_an_existing_old_key_per_scope(self) -> None:
        self._seed_before((("", _OLD_KEY, "2"), ("t3-teatree", _OLD_KEY, "9")))
        self._migrate_and_read()
        config_setting = (
            MigrationExecutor(connection)
            .loader.project_state(_AFTER)
            .apps.get_model(
                "core",
                "ConfigSetting",
            )
        )
        config_setting.objects.create(scope="", key=_OLD_KEY, value="11")

        rows = self._migrate_and_read(_BEFORE)

        assert rows == {("", _OLD_KEY): "11", ("t3-teatree", _OLD_KEY): "9"}

    def test_the_historical_mapping_is_independent_of_the_live_retirement_registry(self) -> None:
        self._seed_before((("", _OLD_KEY, "2"),))

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setitem(retired_settings.RENAMED_SETTING_KEYS, _OLD_KEY, "future_name")
            rows = self._migrate_and_read()

        assert rows == {("", _NEW_KEY): "2"}
