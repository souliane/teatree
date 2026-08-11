"""Activating a halt mode leaves the load-bearing tier ticking (#4188).

The box took two out-of-memory emergencies in one day and ``resource_pressure`` /
``idle_stack_reaper`` recovered it both times — so the mode an operator reaches for during
an incident must not be the one that stops them. Asserted on the ACTUAL admission verdict
and on chain membership rather than on the mask's contents: a row saying ``true`` proves
nothing about whether anything drives the loop (#4185).

Every ``Loop`` column is OFF here, so the mask is the only thing that can admit the tier —
an assertion that passed on the base flags would prove nothing about the mode.
"""

import django.test
from django.utils import timezone

from teatree.core.models import Loop, Mode, ModeOverride
from teatree.loops.chain_membership import timer_chain_loop_names
from teatree.loops.loop_table import admitted_loop_names
from teatree.loops.mode_shape import BACKUP_LOOP, LOAD_BEARING_LOOPS
from teatree.loops.preset_seed import default_preset_specs

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}
_HALT_MODES = ("off",)


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestHaltModesKeepTheBoxAlive(django.test.TestCase):
    """The shipped ``off`` mask, judged by what it actually admits."""

    def setUp(self) -> None:
        Loop.objects.all().delete()
        for name in (*LOAD_BEARING_LOOPS, BACKUP_LOOP, "review"):
            Loop.objects.create(name=name, script=f"src/teatree/loops/{name}/loop.py", delay_seconds=60, enabled=False)
        self.specs = {spec.name: spec for spec in default_preset_specs()}
        self.now = timezone.now()

    def _activate(self, name: str) -> None:
        spec = self.specs[name]
        Mode.objects.update_or_create(
            name=name,
            defaults={"entries": spec.entries},
        )
        ModeOverride.objects.set_override(name)
        self.addCleanup(ModeOverride.objects.clear)

    def test_each_halt_mode_admits_every_load_bearing_loop(self) -> None:
        for name in _HALT_MODES:
            with self.subTest(mode=name):
                self._activate(name)
                assert set(admitted_loop_names(self.now)) >= set(LOAD_BEARING_LOOPS)

    def test_each_halt_mode_leaves_every_load_bearing_loop_carrying_a_timer_chain(self) -> None:
        for name in _HALT_MODES:
            with self.subTest(mode=name):
                self._activate(name)
                assert timer_chain_loop_names(self.now) >= set(LOAD_BEARING_LOOPS)

    def test_each_halt_mode_still_stops_the_work_loops(self) -> None:
        """The halt half of the contract: only the tier survives, never the factory."""
        for name in _HALT_MODES:
            with self.subTest(mode=name):
                self._activate(name)
                assert "review" not in admitted_loop_names(self.now)

    def test_no_halt_mode_keeps_writing_backups(self) -> None:
        for name in _HALT_MODES:
            with self.subTest(mode=name):
                self._activate(name)
                assert BACKUP_LOOP not in admitted_loop_names(self.now)
