"""The #3159 empty-table no-op invariant at the loop-admission level.

With no presets, no active schedule, and no override, every loop's admission is
byte-for-byte the pre-#3159 two-plane verdict (``Loop.enabled`` AND not
``LoopState``-held). A NON-activated preset (a row with entries but no override /
active schedule) never changes admission — only activation does. This mirrors the
#1913/#1775 no-regression shape at the level that actually gates the fleet.
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopPreset, LoopState
from teatree.loop.loop_state_db import loop_state_admits
from teatree.loops.loop_table import admitted_loop_names
from teatree.loops.registry import iter_loops
from teatree.loops.seed import seed_default_loops_and_prompts


def _interval_loop_names() -> list[str]:
    # Registry loops that are due immediately when never run (interval, not daily,
    # not off_live_tick) — the set admission can actually return.
    return [loop.name for loop in iter_loops() if not loop.off_live_tick][:6]


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestEmptyTableNoOpInvariant(django.test.TestCase):
    def setUp(self) -> None:
        # Ensure the registry Loop rows exist in both migration modes, then toggle a
        # representative spread WITHOUT recreating rows (an update never trips the
        # prompt-xor-script constraint a fresh create would on a prompt-backed loop).
        seed_default_loops_and_prompts()
        self.names = _interval_loop_names()
        if self.names:
            Loop.objects.filter(name=self.names[0]).update(enabled=False, last_run_at=None)
        if len(self.names) >= 2:
            LoopState.objects.pause(self.names[1])

    def _base_expected(self, now: dt.datetime) -> set[str]:
        held = set(LoopState.objects.held_names())
        due_registry = {loop.name for loop in iter_loops() if not loop.off_live_tick}
        rows = {row.name: row for row in Loop.objects.all()}
        return {
            name
            for name in self.names
            if name in due_registry
            and (row := rows.get(name)) is not None
            and row.is_due(now)
            and loop_state_admits(configured_enabled=row.enabled, held=name in held, preset_state=None, forced=None)
        }

    def test_admission_matches_base_two_plane_verdict(self) -> None:
        now = timezone.now()
        assert set(admitted_loop_names(now)) & set(self.names) == self._base_expected(now)

    def test_non_activated_preset_does_not_change_admission(self) -> None:
        now = timezone.now()
        before = set(admitted_loop_names(now)) & set(self.names)
        # A preset that would force everything off — but it is NOT activated
        # (no override, no active schedule), so admission is unchanged.
        LoopPreset.objects.create(name="off", entries=dict.fromkeys(self.names, False))
        after = set(admitted_loop_names(now)) & set(self.names)
        assert after == before == self._base_expected(now)
