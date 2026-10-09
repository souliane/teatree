"""teatree.core.mode_resolution — the unified operating-mode resolver (#61, #4202).

Proves the override → schedule → default precedence, the per-loop ``.state_for`` opinion a
resolved mode carries, the fail-open verdict when nothing resolves, and the refusal to
override onto a mode name no row carries. Integration-first against the real DB.
"""

import django.test
import pytest

from teatree.core import mode_resolution
from teatree.core.mode_resolution import clear_mode_override, resolve_active_mode, set_mode_override
from teatree.core.models import ConfigSetting, Mode, ModeOverride

#: Names the #4202 collapse retired. An override onto one must refuse rather than
#: silently fall open to the configured default.
RETIRED_MODES = ("engaged", "heads-down", "low-power", "unattended", "offline")


class _EmptyModeTables(django.test.TestCase):
    """Resolve against only the rows each test creates, not the seeded fleet."""

    def setUp(self) -> None:
        super().setUp()
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestResolveActiveMode(_EmptyModeTables):
    def setUp(self) -> None:
        super().setUp()
        self.present = Mode.objects.create(name="present", entries={"review": True})
        self.afk = Mode.objects.create(name="afk", entries={"review": False})

    def test_default_when_no_override_is_the_configured_default_mode(self) -> None:
        resolved = resolve_active_mode()
        assert resolved.source == "default"
        assert resolved.name == "present"
        assert resolved.state_for("review") is True

    def test_manual_override_wins_and_carries_the_per_loop_opinion(self) -> None:
        set_mode_override("afk", reason="test override")
        resolved = resolve_active_mode()
        assert resolved.source == "override"
        assert resolved.name == "afk"
        assert resolved.state_for("review") is False

    def test_a_loop_the_resolved_mode_does_not_name_reads_off(self) -> None:
        assert resolve_active_mode().state_for("never-declared") is False

    def test_missing_default_mode_fails_open_to_admitting_every_loop(self) -> None:
        Mode.objects.all().delete()
        resolved = resolve_active_mode()
        assert resolved.name == mode_resolution.FALLBACK_DEFAULT_MODE
        assert resolved.fail_open is True
        # A real row answers for every loop and an unnamed one reads OFF. A resolution
        # that could not answer at all must not inherit that, or a broken config would
        # mask the whole fleet instead of failing open.
        assert resolved.state_for("anything") is True


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestOverrideRefusesAnUnknownMode(_EmptyModeTables):
    """A dangling override name would silently fall open to base config — refuse it."""

    def setUp(self) -> None:
        super().setUp()
        Mode.objects.create(name="present", entries={})

    def test_a_retired_preset_name_resolves_to_nothing(self) -> None:
        for name in RETIRED_MODES:
            with self.subTest(name=name), pytest.raises(LookupError):
                set_mode_override(name, reason="test override")
        assert not ModeOverride.objects.exists()

    def test_a_defined_mode_is_accepted(self) -> None:
        set_mode_override("present", reason="test override")
        assert resolve_active_mode().name == "present"


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestClearingTheOverride(_EmptyModeTables):
    def setUp(self) -> None:
        super().setUp()
        Mode.objects.create(name="present", entries={})
        Mode.objects.create(name="afk", entries={})
        ConfigSetting.objects.set_value("default_mode", "afk")

    def test_clearing_the_override_returns_to_the_default_mode(self) -> None:
        set_mode_override("present", reason="test override")
        assert clear_mode_override() is True
        assert resolve_active_mode().name == "afk"
