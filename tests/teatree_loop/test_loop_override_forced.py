"""Tri-state emergency FORCED layer over the preset mask (#3248).

``t3 loop override <name> on|off`` writes a per-loop FORCED value on
``LoopState`` that beats a preset force-off (the gap the 4-layer verdict could
not express) while a durable hold still beats everything. Resolution order:
hold > forced > preset > base.
"""

import datetime as dt

import pytest
from django.utils import timezone

from teatree.core.models import LoopState
from teatree.core.models.loop_state import ForcedState, row_forced_value
from teatree.loop.loop_state_db import control_planes_in_db, forced_loop_map, loop_forced_in_db, loop_state_admits

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestAdmissionMatrix:
    def test_hold_beats_forced_on(self) -> None:
        # The emergency brake (a hold) still wins over a manual force-on.
        assert loop_state_admits(configured_enabled=True, held=True, preset_state=True, forced=True) is False

    def test_forced_on_beats_preset_off(self) -> None:
        # The gap the tri-state layer closes: a manual force-on overrides a
        # preset that forces the loop off.
        assert loop_state_admits(configured_enabled=False, held=False, preset_state=False, forced=True) is True

    def test_forced_off_beats_preset_on(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=False, preset_state=True, forced=False) is False

    def test_neutral_falls_through_to_preset(self) -> None:
        assert loop_state_admits(configured_enabled=False, held=False, preset_state=True, forced=None) is True

    def test_neutral_no_preset_falls_through_to_base(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=False, preset_state=None, forced=None) is True
        assert loop_state_admits(configured_enabled=False, held=False, preset_state=None, forced=None) is False


class TestOverridePersistence:
    def test_override_on_sets_forced_true(self) -> None:
        LoopState.objects.override("review", on=True, reason="incident")
        assert LoopState.objects.forced_of("review") is True

    def test_override_off_sets_forced_false(self) -> None:
        LoopState.objects.override("news", on=False)
        assert LoopState.objects.forced_of("news") is False

    def test_absent_row_is_neutral(self) -> None:
        assert LoopState.objects.forced_of("never-touched") is None

    def test_clear_override_returns_to_neutral(self) -> None:
        LoopState.objects.override("review", on=True)
        LoopState.objects.clear_override("review")
        assert LoopState.objects.forced_of("review") is None

    def test_expired_ttl_resolves_neutral(self) -> None:
        past = timezone.now() - dt.timedelta(hours=1)
        LoopState.objects.override("review", on=True, until=past)
        assert LoopState.objects.forced_of("review") is None

    def test_live_ttl_resolves_forced(self) -> None:
        future = timezone.now() + dt.timedelta(hours=1)
        LoopState.objects.override("review", on=True, until=future)
        assert LoopState.objects.forced_of("review") is True

    def test_forced_map_excludes_neutral_and_expired(self) -> None:
        LoopState.objects.override("on-loop", on=True)
        LoopState.objects.override("off-loop", on=False)
        LoopState.objects.override("gone", on=True, until=timezone.now() - dt.timedelta(minutes=1))
        LoopState.objects.pause("held-loop")  # a hold, not a forced value
        forced = LoopState.objects.forced_map()
        assert forced == {"on-loop": True, "off-loop": False}

    def test_override_leaves_hold_status_untouched(self) -> None:
        # The forced plane and the hold plane are orthogonal — an override does
        # not clear an existing PAUSE.
        LoopState.objects.pause("review")
        LoopState.objects.override("review", on=True)
        assert LoopState.objects.is_paused("review") is True
        assert LoopState.objects.forced_of("review") is True


class TestRowForcedValue:
    def test_neutral_and_absent_resolve_none(self) -> None:
        assert row_forced_value(ForcedState.NEUTRAL.value, None) is None
        assert row_forced_value(None, None) is None

    def test_on_off_resolve_bool(self) -> None:
        assert row_forced_value(ForcedState.ON.value, None) is True
        assert row_forced_value(ForcedState.OFF.value, None) is False

    def test_expired_ttl_resolves_none(self) -> None:
        past = timezone.now() - dt.timedelta(minutes=1)
        assert row_forced_value(ForcedState.ON.value, past) is None


class TestForcedDbReads:
    def test_loop_forced_in_db_reads_the_forced_plane(self) -> None:
        LoopState.objects.override("review", on=True)
        assert loop_forced_in_db("review") is True
        assert loop_forced_in_db("never-touched") is None

    def test_forced_loop_map_bulk_reads_live_forced(self) -> None:
        LoopState.objects.override("on-loop", on=True)
        LoopState.objects.override("off-loop", on=False)
        assert forced_loop_map() == {"on-loop": True, "off-loop": False}

    def test_control_planes_returns_held_and_forced(self) -> None:
        LoopState.objects.pause("held-loop")
        LoopState.objects.override("forced-loop", on=True)
        held, forced = control_planes_in_db()
        assert "held-loop" in held
        assert forced == {"forced-loop": True}
