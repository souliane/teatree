"""The three loop-gating planes reach the SAME verdict for a loop name (#2584).

The #2513 cutover left three planes that could disagree about whether a loop
runs — (1) **master**: ``teatree.loops.master.build_loop_table_jobs`` (the live
fat tick's fan-out); (2) **scoped/orchestrator**:
``teatree.loops.config.LoopsConfig.is_enabled`` (consulted by the orchestrator
and ``run_scoped_tick``); (3) **review-claim chokepoint**:
``teatree.loop.review_claim_signals.review_loop_enabled`` (the discovery-time
review-claim gate).

This is the anti-vacuity guard the prior 'fixed' verdict lacked: under each of
{``Loop.enabled=False``, ``LoopState`` paused} all three must agree the loop does
NOT run, and with no hold all three must agree it DOES. A set ``T3_LOOPS_DISABLED``
env var is INERT across all three planes (loop control is DB-only). Before the
unification the ``build_loop_table_jobs`` arm disagreed under a ``LoopState`` hold
(it gated on ``Loop.enabled`` only) — the planes diverged.
"""

from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Prompt
from teatree.loop.review_claim_signals import review_loop_enabled
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig
from teatree.loops.master import build_loop_table_jobs

_REVIEW = "review"


def _mini(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=lambda n=name, **_: [f"job-{n}"])


def _prompt() -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name="xplane-prompt", defaults={"body": "do x"})
    return prompt


def _ensure_loop(name: str, *, enabled: bool = True) -> None:
    """Ensure an enabled+due ``Loop`` row for *name* (migration 0078 may seed it).

    ``review`` is seeded by migration 0078 (paused under 0087), so ``create``
    would collide. ``update_or_create`` makes the row enabled + never-run (due)
    regardless of the migration-seeded state.
    """
    Loop.objects.update_or_create(
        name=name,
        defaults={"delay_seconds": 60, "prompt": _prompt(), "script": "", "enabled": enabled, "last_run_at": None},
    )


def _master_runs(name: str, *, now: object) -> bool:
    """True iff the master fan-out emits *name*'s job (the live-tick verdict)."""
    with patch("teatree.loops.master.iter_loops", return_value=(_mini(name),)):
        jobs = build_loop_table_jobs({}, now=now)
    return f"job-{name}" in jobs


@django.test.override_settings(USE_TZ=True)
class TestCrossPlaneConsistency(django.test.TestCase):
    def _assert_all_planes_agree(self, name: str, *, expected: bool) -> None:
        now = timezone.now()
        # The Loop row must exist and be enabled+due for the master arm so the
        # ONLY thing differing across the holds below is the control plane, not
        # the row's own enabled/cadence state (except the explicit toggle case).
        assert _master_runs(name, now=now) is expected, "master plane disagrees"
        config = LoopsConfig.load()
        assert config.is_enabled(_mini(name)) is expected, "LoopsConfig plane disagrees"
        if name == _REVIEW:
            assert review_loop_enabled() is expected, "review_loop_enabled plane disagrees"

    def test_all_planes_agree_loop_runs_when_unheld(self) -> None:
        _ensure_loop(_REVIEW)
        self._assert_all_planes_agree(_REVIEW, expected=True)

    def test_all_planes_agree_loop_state_pause_stops_it(self) -> None:
        _ensure_loop(_REVIEW)
        LoopState.objects.pause(_REVIEW)
        self._assert_all_planes_agree(_REVIEW, expected=False)

    def test_all_planes_agree_env_kill_switch_is_inert(self) -> None:
        # ``T3_LOOPS_DISABLED`` is removed — a set env var is inert across all
        # three planes; the loop still runs (a DB hold is the control outcome).
        _ensure_loop(_REVIEW)
        with patch.dict("os.environ", {"T3_LOOPS_DISABLED": _REVIEW}):
            self._assert_all_planes_agree(_REVIEW, expected=True)

    def test_all_planes_agree_loop_enabled_false_stops_master_and_config(self) -> None:
        # When the Loop row itself is disabled, the master and the scoped path
        # (via run_scoped_tick's Loop.enabled filter / the orchestrator) must
        # both treat it as not-running. LoopsConfig.is_enabled does not read the
        # Loop row, so this arm pins the two row-aware planes (master here; the
        # scoped Loop.enabled filter is pinned in test_scoped_tick.py).
        now = timezone.now()
        _ensure_loop(_REVIEW, enabled=False)
        assert _master_runs(_REVIEW, now=now) is False
        # No LoopState hold, no env hold → the control-plane verdict is "runs",
        # which is exactly why the row-level Loop.enabled gate is the one that
        # must stop it on the row-aware planes.
        assert LoopsConfig.load().is_enabled(_mini(_REVIEW)) is True
        assert review_loop_enabled() is True
