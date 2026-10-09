"""``loops_tick`` full-fleet scan scoping, off the preset model and onto the box.

`scanner_overlay_scope` restricts the full-fleet scan to the named backends; every
fallback (no setting, empty scope, no matching overlay) scans the whole fleet, because a
typo must not silently stop the factory.

The last test is the one that made the relocation worth doing: the scope must be the
same whichever preset is active. It fails against the preset-carried version, where a
preset with no scope of its own widened the sweep to the whole fleet the moment the
operator went AFK.
"""

from unittest.mock import patch

import django.test

from teatree.core.backend_factory import OverlayBackends
from teatree.core.management.commands.loops_tick import _focus_scoped_backends
from teatree.core.models import ConfigSetting, Mode, ModeOverride

_FLEET = [OverlayBackends(name="primary"), OverlayBackends(name="dayjob")]
_PATCH_FLEET = "teatree.core.management.commands.loops_tick.iter_overlay_backends"


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestFocusScopedBackends(django.test.TestCase):
    def _scope(self, scope: list[str]) -> None:
        ConfigSetting.objects.set_value("scanner_overlay_scope", scope)

    def test_no_setting_scans_the_whole_fleet(self) -> None:
        with patch(_PATCH_FLEET, return_value=_FLEET):
            assert _focus_scoped_backends() == _FLEET

    def test_scope_restricts_to_the_named_backend(self) -> None:
        self._scope(["primary"])
        with patch(_PATCH_FLEET, return_value=_FLEET):
            scoped = _focus_scoped_backends()
        assert [backend.name for backend in scoped] == ["primary"]

    def test_empty_scope_scans_the_whole_fleet(self) -> None:
        self._scope([])
        with patch(_PATCH_FLEET, return_value=_FLEET):
            assert _focus_scoped_backends() == _FLEET

    def test_non_matching_scope_falls_back_to_whole_fleet(self) -> None:
        self._scope(["ghost-overlay"])
        with patch(_PATCH_FLEET, return_value=_FLEET):
            assert _focus_scoped_backends() == _FLEET

    def test_an_unreadable_setting_scans_the_whole_fleet_rather_than_nothing(self) -> None:
        target = "teatree.core.management.commands.loops_tick.get_effective_settings"
        with patch(_PATCH_FLEET, return_value=_FLEET), patch(target, side_effect=RuntimeError("db down")):
            assert _focus_scoped_backends() == _FLEET

    def test_the_scope_does_not_change_when_the_active_preset_does(self) -> None:
        self._scope(["primary"])
        Mode.objects.create(name="afk", entries={})
        ModeOverride.objects.set_override("afk", reason="test override")
        with patch(_PATCH_FLEET, return_value=_FLEET):
            scoped = _focus_scoped_backends()
        assert [backend.name for backend in scoped] == ["primary"]
