"""teatree.core.mode_resolution — the unified operating-mode resolver (#61, #4202).

Proves the override→schedule→default precedence, the per-loop ``.state_for`` opinion a
resolved mode carries, the presence upgrade, and the refusal to override onto a mode
name no row carries. Integration-first against the real DB.
"""

import datetime as dt
import tempfile
from pathlib import Path
from unittest import mock

import django.test
import pytest
from django.utils import timezone

from teatree.core import mode_resolution
from teatree.core.mode_resolution import clear_mode_override, resolve_active_mode, set_mode_override
from teatree.core.models import ConfigSetting, Mode, ModeOverride
from teatree.live_presence import PresenceHeartbeat

#: Names the #4202 collapse retired. An override onto one must refuse rather than
#: silently fall open to the configured default.
RETIRED_MODES = ("engaged", "heads-down", "low-power", "unattended", "offline")


class _TmpStateMixin(django.test.TestCase):
    """Repoint the presence heartbeat to a per-test tmp dir.

    The resolver reads the live-presence heartbeat, which defaults to the shared
    primary data dir. Redirecting it keeps a fixed-``now`` resolver test from reading
    a stray future-dated keystroke as fresh.
    """

    def setUp(self) -> None:
        super().setUp()
        tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(mode_resolution, "PRESENCE", PresenceHeartbeat(lambda: tmp / "presence"))
        patcher.start()
        self.addCleanup(patcher.stop)
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestResolveActiveMode(_TmpStateMixin):
    def setUp(self) -> None:
        super().setUp()
        self.present = Mode.objects.create(name="present", entries={"review": True})
        self.away = Mode.objects.create(name="away", entries={"review": False})

    def test_default_when_no_override_is_the_configured_default_mode(self) -> None:
        resolved = resolve_active_mode()
        assert resolved.source == "default"
        assert resolved.name == "present"
        assert resolved.state_for("review") is True

    def test_manual_override_wins_and_carries_the_per_loop_opinion(self) -> None:
        set_mode_override("away")
        resolved = resolve_active_mode()
        assert resolved.source == "override"
        assert resolved.name == "away"
        assert resolved.state_for("review") is False

    def test_missing_default_mode_fails_open_to_no_opinion(self) -> None:
        Mode.objects.all().delete()
        resolved = resolve_active_mode()
        assert resolved.name == mode_resolution.FALLBACK_DEFAULT_MODE
        assert resolved.state_for("anything") is None  # no opinion → inherit base


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestOverrideRefusesAnUnknownMode(_TmpStateMixin):
    """A dangling override name would silently fall open to base config — refuse it."""

    def setUp(self) -> None:
        super().setUp()
        Mode.objects.create(name="present", entries={})

    def test_a_retired_preset_name_resolves_to_nothing(self) -> None:
        for name in RETIRED_MODES:
            with self.subTest(name=name), pytest.raises(LookupError):
                set_mode_override(name)
        assert not ModeOverride.objects.exists()

    def test_a_defined_mode_is_accepted(self) -> None:
        set_mode_override("present")
        assert resolve_active_mode().name == "present"


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestPresenceUpgrade(_TmpStateMixin):
    def setUp(self) -> None:
        super().setUp()
        Mode.objects.create(name="present", entries={})
        Mode.objects.create(name="away", entries={})
        ConfigSetting.objects.set_value("default_mode", "away")

    def _stamp_keystroke(self, *, ago: dt.timedelta) -> None:
        mode_resolution.PRESENCE.record(session_id="s", now=timezone.now() - ago)

    def test_fresh_keystroke_upgrades_a_default_away_mode(self) -> None:
        self._stamp_keystroke(ago=dt.timedelta(minutes=1))
        resolved = resolve_active_mode()
        assert resolved.source == "live"
        assert resolved.name == "present"

    def test_stale_keystroke_does_not_upgrade(self) -> None:
        self._stamp_keystroke(ago=dt.timedelta(hours=2))
        resolved = resolve_active_mode()
        assert resolved.source == "default"
        assert resolved.name == "away"

    def test_manual_override_is_never_upgraded_by_presence(self) -> None:
        """The override is how an operator pins a mode a keystroke must not lift."""
        set_mode_override("away")
        self._stamp_keystroke(ago=dt.timedelta(minutes=1))
        resolved = resolve_active_mode()
        assert resolved.source == "override"
        assert resolved.name == "away"

    def test_the_upgrade_target_itself_is_not_re_upgraded(self) -> None:
        ConfigSetting.objects.set_value("default_mode", "present")
        self._stamp_keystroke(ago=dt.timedelta(minutes=1))
        resolved = resolve_active_mode()
        assert resolved.source == "default"
        assert resolved.name == "present"

    def test_clearing_the_override_returns_to_the_default_mode(self) -> None:
        set_mode_override("present")
        assert clear_mode_override() is True
        assert resolve_active_mode().name == "away"
