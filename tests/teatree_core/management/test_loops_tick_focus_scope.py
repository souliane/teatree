"""``loops_tick`` focus-preset overlay scoping (#3159 item 7).

A focus preset's ``overlay_scope`` restricts the full-fleet scan to that backend;
every fallback (no preset, empty scope, no matching overlay) scans the whole fleet.
"""

from unittest.mock import patch

import django.test

from teatree.core.backend_factory import OverlayBackends
from teatree.core.management.commands.loops_tick import _focus_scoped_backends
from teatree.core.models import LoopPreset, LoopPresetOverride

_FLEET = [OverlayBackends(name="primary"), OverlayBackends(name="dayjob")]


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestFocusScopedBackends(django.test.TestCase):
    def _activate(self, scope: list[str]) -> None:
        LoopPreset.objects.create(name="focus:primary", entries={}, overlay_scope=scope)
        LoopPresetOverride.objects.set_override("focus:primary")

    def test_no_preset_scans_the_whole_fleet(self) -> None:
        with patch("teatree.core.management.commands.loops_tick.iter_overlay_backends", return_value=_FLEET):
            assert _focus_scoped_backends() == _FLEET

    def test_scope_restricts_to_the_named_backend(self) -> None:
        self._activate(["primary"])
        with patch("teatree.core.management.commands.loops_tick.iter_overlay_backends", return_value=_FLEET):
            scoped = _focus_scoped_backends()
        assert [backend.name for backend in scoped] == ["primary"]

    def test_empty_scope_scans_the_whole_fleet(self) -> None:
        self._activate([])
        with patch("teatree.core.management.commands.loops_tick.iter_overlay_backends", return_value=_FLEET):
            assert _focus_scoped_backends() == _FLEET

    def test_non_matching_scope_falls_back_to_whole_fleet(self) -> None:
        self._activate(["ghost-overlay"])
        with patch("teatree.core.management.commands.loops_tick.iter_overlay_backends", return_value=_FLEET):
            assert _focus_scoped_backends() == _FLEET
