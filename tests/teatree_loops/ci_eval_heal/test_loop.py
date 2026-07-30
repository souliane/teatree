"""CI-eval heal mini-loop package (#3201 PR-3a) — MINI_LOOP shape + default-OFF seed."""

from django.test import TestCase

from teatree.loops.ci_eval_heal.loop import MINI_LOOP
from teatree.loops.seed import DEFAULT_LOOPS


class TestMiniLoop:
    def test_name_matches_package_directory(self) -> None:
        assert MINI_LOOP.name == "ci_eval_heal"

    def test_runs_on_the_live_tick_at_five_minutes(self) -> None:
        # Observe polling belongs on the live tick (not off_live_tick like dream) so
        # an in-flight run is re-polled promptly once enabled.
        assert MINI_LOOP.off_live_tick is False
        assert MINI_LOOP.default_cadence_seconds == 300

    def test_build_jobs_is_empty_when_no_scanner(self) -> None:
        # With no open sessions the scanner still builds, but the job list is a
        # single scanner job (the scanner itself returns no signal when idle).
        jobs = MINI_LOOP.build_jobs()
        assert len(jobs) == 1
        assert jobs[0].scanner.name == "ci_eval_heal"


class TestDefaultOff(TestCase):
    def test_seed_spec_ships_disabled(self) -> None:
        spec = next(s for s in DEFAULT_LOOPS if s.name == "ci_eval_heal")
        assert spec.default_enabled is False
        assert spec.colleague_facing is False
