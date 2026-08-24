"""The ``0066`` data migration re-admits the load-bearing tier on rows written before it (#4188).

The live ``off`` row masked ``resource_pressure`` / ``idle_stack_reaper`` off while
``db_backup`` stayed forced ON — the mask that can only ever consume disk. The seed is
``get_or_create``, so the corrected shipped table never reaches an existing row.
Anti-vacuous: dropping the ``RunPython`` leaves ``resource_pressure`` on ``False`` and this
goes RED.
"""

from dataclasses import dataclass

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0065_interactivedispatch")
_AFTER = ("core", "0066_admit_load_bearing_loops")

#: The live ``off`` row as measured on the box that took two out-of-memory emergencies.
_LIVE_OFF = {
    "inbox": False,
    "idle_stack_reaper": False,
    "local_stack_queue": False,
    "resource_pressure": False,
    "housekeeping": False,
    "review": False,
    "db_backup": True,
}


@dataclass(frozen=True, slots=True)
class HealedMode:
    """One mode row's post-migration mask and operator-facing line."""

    entries: dict[str, bool]
    description: str


@pytest.mark.timeout(240)
class TestAdmitLoadBearingLoops(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _migrate_and_read(names: tuple[str, ...]) -> dict[str, HealedMode]:
        """Each named mode's post-migration mask and description."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_AFTER])
        mode = MigrationExecutor(connection).loader.project_state([_AFTER]).apps.get_model("core", "Mode")
        rows = {name: mode.objects.get(name=name) for name in names}
        return {name: HealedMode(entries=row.entries, description=row.description) for name, row in rows.items()}

    def _seed_before(
        self, rows: dict[str, dict[str, bool]], *, low_power_pin: str = "", descriptions: dict[str, str] | None = None
    ) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        old_apps = executor.loader.project_state(_BEFORE).apps
        mode = old_apps.get_model("core", "Mode")
        mode.objects.all().delete()
        for name, entries in rows.items():
            mode.objects.create(name=name, entries=entries, description=(descriptions or {}).get(name, ""))
        if low_power_pin:
            config_setting = old_apps.get_model("core", "ConfigSetting")
            config_setting.objects.update_or_create(
                scope="", key="low_power_preset_name", defaults={"value": low_power_pin}
            )

    def test_the_live_off_row_is_re_admitted_and_its_work_mask_is_untouched(self) -> None:
        self._seed_before({"off": dict(_LIVE_OFF)})

        entries = self._migrate_and_read(("off",))["off"].entries

        assert entries["resource_pressure"] is True
        assert entries["idle_stack_reaper"] is True
        assert entries["inbox"] is True
        assert entries["local_stack_queue"] is True
        assert entries["housekeeping"] is True
        assert entries["review"] is False
        assert entries["db_backup"] is True

    def test_the_low_power_mode_keeps_its_token_budget_mask(self) -> None:
        self._seed_before({"low-power": {"resource_pressure": False, "news": False}})

        entries = self._migrate_and_read(("low-power",))["low-power"].entries

        assert entries["resource_pressure"] is False

    def test_the_escape_follows_the_pinned_setting_not_the_shipped_name(self) -> None:
        rows = {
            "token-guard": {"resource_pressure": False},
            "low-power": {"resource_pressure": False},
        }
        self._seed_before(rows, low_power_pin="token-guard")

        rows = self._migrate_and_read(("token-guard", "low-power"))

        assert rows["token-guard"].entries["resource_pressure"] is False
        assert rows["low-power"].entries["resource_pressure"] is True

    def test_an_absent_entry_stays_absent_rather_than_being_forced(self) -> None:
        """Absent is inherit — the migration heals a QUIETING, it does not write new opinions."""
        self._seed_before({"engaged": {"review": True}})

        entries = self._migrate_and_read(("engaged",))["engaged"].entries

        assert entries == {"review": True}

    def test_a_stale_every_loop_off_description_is_restated(self) -> None:
        """The heal makes "every loop off" untrue, so the operator-facing line is corrected."""
        shipped = "Holiday: every loop off, questions defer AND the self-pump pauses (was 'off' preset + 'away')."
        self._seed_before({"offline": dict(_LIVE_OFF)}, descriptions={"offline": shipped})

        description = self._migrate_and_read(("offline",))["offline"].description

        assert description != shipped
        assert "every WORK loop off" in description

    def test_an_operator_written_description_is_left_alone(self) -> None:
        self._seed_before({"offline": dict(_LIVE_OFF)}, descriptions={"offline": "my own words"})

        description = self._migrate_and_read(("offline",))["offline"].description

        assert description == "my own words"
