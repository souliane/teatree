"""The ``0067`` data migration collapses the seven pre-decision modes to five (#4202).

Every name a stored row can point at travels with the rename — schedule slots, the
manual override, the mode-valued ``ConfigSetting`` rows and the renamed holiday
schedule — so no layer is left dangling at a mode that no longer exists. A dangling
value is the failure this pins: it falls open to base config rather than erroring.

Anti-vacuous: dropping the ``RunPython`` leaves every row under its old name and each
test below goes RED.
"""

import datetime as dt
from dataclasses import dataclass, field

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0066_admit_load_bearing_loops")
_AFTER = ("core", "0067_collapse_modes_to_five_presets")

#: The seven live rows as the issue measured them, reduced to the columns that matter here.
_LIVE_MODES = ("engaged", "heads-down", "low-power", "maintenance", "off", "offline", "unattended")

_SHIPPED_MAINTENANCE = "Nights: self-maintenance + self-improvement only, no ticket/colleague/delivery work."


@dataclass(frozen=True, slots=True)
class StoredRefs:
    """Everything a stored row can point AT a mode (or the renamed schedule) with."""

    slots: tuple[str, ...] = ()
    override: tuple[str, str] | None = None
    settings: dict[str, str] = field(default_factory=dict)
    schedule_name: str = ""


@pytest.mark.timeout(240)
class TestCollapseModesToFivePresets(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def _old_apps(self):
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        return executor.loader.project_state(_BEFORE).apps

    def _seed_before(
        self,
        *,
        modes: tuple[str, ...] = _LIVE_MODES,
        descriptions: dict[str, str] | None = None,
        maintenance_entries: dict[str, bool] | None = None,
        refs: StoredRefs = StoredRefs(),  # noqa: B008 — frozen, so one shared instance is safe
    ) -> None:
        apps = self._old_apps()
        mode = apps.get_model("core", "Mode")
        mode.objects.all().delete()
        for name in modes:
            entries = dict(maintenance_entries or {}) if name == "maintenance" else {}
            mode.objects.create(name=name, entries=entries, description=(descriptions or {}).get(name, ""))
        if refs.schedule_name:
            schedule = apps.get_model("core", "ModeSchedule")
            schedule.objects.all().delete()
            row = schedule.objects.create(
                name=refs.schedule_name, description="The holiday calendar: unattended all week."
            )
            slot_model = apps.get_model("core", "ModeScheduleSlot")
            for preset_name in refs.slots:
                slot_model.objects.create(
                    schedule=row, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name=preset_name
                )
        if refs.override is not None:
            preset_name, reason = refs.override
            apps.get_model("core", "ModeOverride").objects.create(preset_name=preset_name, reason=reason)
        for key, value in refs.settings.items():
            apps.get_model("core", "ConfigSetting").objects.update_or_create(
                scope="", key=key, defaults={"value": value}
            )

    @staticmethod
    def _after():
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_AFTER])
        return MigrationExecutor(connection).loader.project_state([_AFTER]).apps

    def _mode_names(self) -> set[str]:
        return set(self._after().get_model("core", "Mode").objects.values_list("name", flat=True))

    def test_the_seven_live_rows_become_the_five_decided_presets(self) -> None:
        self._seed_before()

        assert self._mode_names() == {"present", "away", "low-token", "maintenance", "off"}

    def test_away_is_absent_when_there_was_no_unattended_row_to_rename(self) -> None:
        """``offline`` merges rather than renames: its all-off mask would starve ``away``.

        The idempotent seed is then the only thing that creates ``away``, which is the
        only source of the mask an intake-taking preset actually needs.
        """
        self._seed_before(modes=("offline", "off"))

        assert self._mode_names() == {"off"}

    def test_the_merged_and_cut_rows_leave_nothing_behind(self) -> None:
        self._seed_before()

        assert not self._mode_names() & {"offline", "heads-down", "engaged", "low-power", "unattended"}

    def test_maintenance_is_redefined_to_drain_rather_than_idle(self) -> None:
        self._seed_before(maintenance_entries={"ship": False, "review": False, "tickets": True, "dream": True})

        entries = self._after().get_model("core", "Mode").objects.get(name="maintenance").entries

        assert entries["ship"] is True
        assert entries["review"] is True
        assert entries["tickets"] is False
        assert entries["issue_implementer"] is False
        assert entries["dream"] is True

    def test_a_shipped_description_is_refreshed_and_an_edited_one_is_kept(self) -> None:
        self._seed_before(descriptions={"maintenance": _SHIPPED_MAINTENANCE, "unattended": "my own words"})

        rows = {row.name: row.description for row in self._after().get_model("core", "Mode").objects.all()}

        assert "Drain-only" in rows["maintenance"]
        assert rows["away"] == "my own words"

    def test_an_operator_row_already_holding_the_successor_name_wins(self) -> None:
        self._seed_before(modes=("engaged", "present"))

        assert self._mode_names() == {"present"}

    def test_every_stored_reference_moves_onto_its_successor(self) -> None:
        self._seed_before(
            refs=StoredRefs(
                slots=("engaged", "unattended", "heads-down"),
                override=("low-power", "auto:low-power (usage window parked)"),
                settings={
                    "default_mode": "engaged",
                    "presence_upgrade_mode": "engaged",
                    "low_power_preset_name": "low-power",
                    "active_loop_schedule": "always-unattended",
                },
                schedule_name="always-unattended",
            )
        )

        apps = self._after()
        slots = set(apps.get_model("core", "ModeScheduleSlot").objects.values_list("preset_name", flat=True))
        override = apps.get_model("core", "ModeOverride").objects.get()
        settings = dict(apps.get_model("core", "ConfigSetting").objects.values_list("key", "value"))

        assert slots == {"present", "away"}
        assert override.preset_name == "low-token"
        assert override.reason == "auto:low-token (usage window parked)"
        assert settings["default_mode"] == "present"
        assert settings["presence_upgrade_mode"] == "present"
        assert settings["low_power_preset_name"] == "low-token"
        assert settings["active_loop_schedule"] == "always-away"
        assert apps.get_model("core", "ModeSchedule").objects.get().name == "always-away"

    def test_a_reference_to_a_cut_mode_is_deleted_rather_than_left_dangling(self) -> None:
        self._seed_before(refs=StoredRefs(override=("heads-down", ""), settings={"default_mode": "heads-down"}))

        apps = self._after()

        assert not apps.get_model("core", "ModeOverride").objects.exists()
        assert not apps.get_model("core", "ConfigSetting").objects.filter(key="default_mode").exists()

    def test_the_three_posture_columns_are_gone_from_the_table(self) -> None:
        self._seed_before()
        self._after()

        with connection.cursor() as cursor:
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(teatree_loop_preset)").fetchall()}

        assert not columns & {"defers_questions", "pauses_self_pump", "presence_sensitive"}
