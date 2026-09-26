"""A posture we could not RESOLVE is not a posture that permits (round-6 findings).

Two halves of one property, both reached through the REAL resolver rather than a
hand-built :class:`ResolvedMode` — a fabricated ``fail_open`` proves the reader, never
that anything ever produces it.

*Reading*: ``resolve_active_preset`` answered ``None`` for a box with no schedule AND for
a dangling override, so a failed read fell to the permitting configured default and spoke
as the owner. *Writing*: the posture is what decides the owner's voice, so setting or
clearing it is an authorization — the governance the retired safety-posture setting
carried follows the decision to its new home.
"""

import datetime as dt
from contextlib import AbstractContextManager
from unittest.mock import patch

import django.test
import pytest
from django.core.exceptions import ValidationError

from teatree.core.mode_resolution import (
    clear_mode_override,
    egress_forbidden,
    owner_voice_forbidden,
    resolve_active_mode,
    set_mode_override,
)
from teatree.core.models import ConfigSetting, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID, RUNNER_PID_ENV, RUNNER_SESSION_ENV
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING

_ANY_LOOP = "followup"


def _permitting_default() -> None:
    """The configured default is ``present``: nothing below a preset forbids anything."""
    Mode.objects.update_or_create(name="present", defaults={"entries": {}, "egress": "allow"})
    ConfigSetting.objects.filter(key=ACTIVE_SCHEDULE_SETTING).delete()
    ModeOverride.objects.all().delete()


def _schedule_with_slot(preset_name: str) -> ModeSchedule:
    schedule = ModeSchedule.objects.create(name="round-the-clock", timezone="UTC")
    ModeScheduleSlot.objects.create(
        schedule=schedule, days=list(range(7)), start_time=dt.time(0, 0), preset_name=preset_name
    )
    ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, schedule.name)
    return schedule


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestAFailedPresetReadIsNotPermissionToPublish(django.test.TestCase):
    """Each arm below resolved to the permitting default before the failure was carried."""

    def setUp(self) -> None:
        _permitting_default()

    def test_a_dangling_override_closes_the_owners_voice(self) -> None:
        ModeOverride.objects.set_override("deleted-preset", reason="names a preset nobody kept")

        assert owner_voice_forbidden() is True

    def test_an_active_schedule_naming_an_unknown_schedule_closes_the_owners_voice(self) -> None:
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "no-such-schedule")

        assert owner_voice_forbidden() is True

    def test_a_schedule_slot_naming_a_deleted_preset_closes_the_owners_voice(self) -> None:
        _schedule_with_slot("deleted-preset")

        assert owner_voice_forbidden() is True

    def test_an_unreadable_resolution_closes_the_owners_voice(self) -> None:
        with patch(
            "teatree.loop.preset_resolution._resolve_active_preset", side_effect=OSError("the DB is unreadable")
        ):
            assert owner_voice_forbidden() is True

    def test_a_failed_read_still_admits_every_loop(self) -> None:
        """Work SELECTION keeps failing open: a broken config must not stop the factory building."""
        ModeOverride.objects.set_override("deleted-preset", reason="names a preset nobody kept")

        assert resolve_active_mode().state_for(_ANY_LOOP) is True
        assert egress_forbidden() is False


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestALegitimateAbsenceStillPermits(django.test.TestCase):
    """The control: the fix must distinguish failure from absence, not close on both."""

    def setUp(self) -> None:
        _permitting_default()

    def test_no_override_and_no_schedule_leaves_the_owners_voice_open(self) -> None:
        assert owner_voice_forbidden() is False

    def test_a_schedule_whose_slots_never_governed_leaves_the_owners_voice_open(self) -> None:
        schedule = ModeSchedule.objects.create(name="never", timezone="UTC")
        ModeScheduleSlot.objects.create(schedule=schedule, days=[], start_time=dt.time(0, 0), preset_name="present")
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, schedule.name)

        assert owner_voice_forbidden() is False

    def test_a_resolved_permitting_preset_leaves_the_owners_voice_open(self) -> None:
        _schedule_with_slot("present")

        assert owner_voice_forbidden() is False


class TestThePostureWriteIsGoverned(django.test.TestCase):
    """`t3 loop preset use present` must not be a self-service permission to speak."""

    def setUp(self) -> None:
        Mode.objects.update_or_create(name="present", defaults={"entries": {}, "egress": "allow"})
        ModeOverride.objects.all().delete()

    @staticmethod
    def _as_unattended_runner() -> AbstractContextManager[None]:
        return patch.dict(
            "os.environ", {RUNNER_SESSION_ENV: LOOP_RUNNER_SESSION_ID, RUNNER_PID_ENV: "4242"}, clear=False
        )

    def test_the_unattended_runner_cannot_select_a_permitting_posture(self) -> None:
        with self._as_unattended_runner(), pytest.raises(ValidationError, match="not an unattended write"):
            set_mode_override("present", reason="opening my own voice")

        assert ModeOverride.objects.current() is None

    def test_the_unattended_runner_cannot_clear_an_override(self) -> None:
        """Clearing exposes whatever schedule or default sits beneath — the same authorization."""
        ModeOverride.objects.set_override("present", reason="set by a session")

        with self._as_unattended_runner(), pytest.raises(ValidationError, match="not an unattended write"):
            clear_mode_override()

        assert ModeOverride.objects.current() is not None

    def test_an_authorized_unattended_transition_is_preserved(self) -> None:
        with self._as_unattended_runner():
            set_mode_override("present", reason="the owner asked for it", authorized_by="jane.doe")

            assert ModeOverride.objects.current().preset_name == "present"

            assert clear_mode_override(authorized_by="jane.doe") is True

    def test_an_attended_session_writes_the_posture_unguarded(self) -> None:
        set_mode_override("present", reason="typed by a human")

        assert ModeOverride.objects.current().preset_name == "present"
