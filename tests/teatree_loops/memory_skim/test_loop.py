"""The memory-skim mini-loop ships weekly, registered, and masked off.

Directive 32 asked for the cadence three times (it restates directives 6 and 2).
This pins the three properties the ask depends on: the loop is discoverable by
the registry fan-out, its cadence is a week, and it is off out of the box — an
operator turns it on deliberately.
"""

from teatree.loops.memory_skim.loop import MINI_LOOP
from teatree.loops.preset_seed import default_preset_specs
from teatree.loops.registry import iter_loops
from teatree.loops.seed import DEFAULT_LOOPS


class TestMemorySkimLoop:
    def test_the_registry_discovers_it(self) -> None:
        assert MINI_LOOP.name in {loop.name for loop in iter_loops()}

    def test_the_cadence_is_a_week_and_a_floor(self) -> None:
        assert MINI_LOOP.default_cadence_seconds == 604800
        assert MINI_LOOP.cadence_is_floor is True

    def test_it_builds_its_scanner_job(self) -> None:
        (job,) = MINI_LOOP.build_jobs()

        assert job.scanner.name == "memory_skim"

    def test_it_ships_disabled(self) -> None:
        spec = next(s for s in DEFAULT_LOOPS if s.name == MINI_LOOP.name)

        assert spec.default_enabled is False
        assert spec.delay_seconds == 604800

    def test_the_off_preset_masks_it(self) -> None:
        off = next(spec for spec in default_preset_specs() if spec.name == "off")

        assert off.entries[MINI_LOOP.name] is False
