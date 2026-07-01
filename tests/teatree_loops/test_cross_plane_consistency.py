"""The loop-gating planes reach a CONSISTENT verdict for a loop name (#2584).

After LOOP-PR-A the loop-run sites share ONE combined verdict
(``teatree.loop.loop_state_db.loop_enabled`` = ``Loop.enabled`` AND not
``LoopState``-held): (1) **fan-out**:
``teatree.loops.loop_table.build_loop_table_jobs`` (the live loop-table fan-out,
composing ``Loop.enabled`` + ``LoopsConfig.is_enabled``); (2) **registration**:
``teatree.loops.claude_specs.enabled_loop_specs`` (the #2650 cron mirror).

Two narrower tiers are layered into / beside that verdict and are pinned here so
the planes can never silently drift apart. (a) ``LoopsConfig.is_enabled`` reads
the durable ``LoopState`` hold ONLY — it does NOT read the ``Loop.enabled``
column, so it is the tier the combined verdict layers on. (b)
``review_claim_signals.review_loop_enabled`` is the discovery-time review-claim
gate: by documented design (#79 / #1913) it resolves through the ``LoopState``
tier ONLY and fails OPEN to enabled — a claim-suppression gate, not a loop-run
decision, so it intentionally tracks the ``LoopState`` arm (not ``Loop.enabled``).

This is the anti-vacuity guard the prior 'fixed' verdict lacked: under a
``LoopState`` pause every plane must agree the loop does NOT run, and with no hold
every plane must agree it DOES. Under ``Loop.enabled=False`` only the row-aware
combined verdict (master) stops it, while the two ``LoopState``-only arms
(LoopsConfig + review-claim) still report "runs" — that divergence is the
designed tier split, asserted explicitly below. A set ``T3_LOOPS_DISABLED`` env
var is INERT (loop control is DB-only).
"""

from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Prompt
from teatree.loop.review_claim_signals import review_loop_enabled
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig
from teatree.loops.loop_table import build_loop_table_jobs

_REVIEW = "review"


def _mini(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=lambda n=name, **_: [f"job-{n}"])


def _prompt() -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name="xplane-prompt", defaults={"body": "do x"})
    return prompt


def _ensure_loop(name: str, *, enabled: bool = True) -> None:
    """Ensure a ``Loop`` row for *name* (a migration may seed it paused).

    ``review`` is seeded by an earlier migration, so ``create`` would collide.
    ``update_or_create`` makes the row match *enabled* + never-run (due)
    regardless of the migration-seeded state.
    """
    Loop.objects.update_or_create(
        name=name,
        defaults={"delay_seconds": 60, "prompt": _prompt(), "script": "", "enabled": enabled, "last_run_at": None},
    )


def _master_runs(name: str, *, now: object) -> bool:
    """True iff the loop-table fan-out emits *name*'s job (the live-tick verdict)."""
    with patch("teatree.loops.loop_table.iter_loops", return_value=(_mini(name),)):
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
        # planes; the loop still runs (a DB hold is the control outcome).
        _ensure_loop(_REVIEW)
        with patch.dict("os.environ", {"T3_LOOPS_DISABLED": _REVIEW}):
            self._assert_all_planes_agree(_REVIEW, expected=True)

    def test_loop_enabled_false_stops_master_but_not_the_loopstate_only_arms(self) -> None:
        # When the Loop row itself is disabled, the row-aware combined verdict
        # (master) must treat it as not-running. The two ``LoopState``-only arms —
        # LoopsConfig.is_enabled and the fail-open review-claim gate — do NOT read
        # ``Loop.enabled``, so they still report "runs". That tier split is the
        # designed behaviour (#79 / #1913): review_loop_enabled is a fail-open
        # claim-suppression gate, not a loop-run decision.
        now = timezone.now()
        _ensure_loop(_REVIEW, enabled=False)
        assert _master_runs(_REVIEW, now=now) is False
        assert LoopsConfig.load().is_enabled(_mini(_REVIEW)) is True
        assert review_loop_enabled() is True
