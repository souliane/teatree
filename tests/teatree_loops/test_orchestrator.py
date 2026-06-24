"""Orchestrator — gate, dispatch, isolate errors, report.

The orchestrator routes each registered mini-loop through three gates:
config (enabled?), cadence (elapsed?), then build_jobs + dispatch +
mark_fired. An error in one loop does not abort the others.
"""

import datetime as dt
import os

from django.test import TestCase

from teatree.core.models import LoopState, MiniLoopMarker
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig
from teatree.loops.orchestrator import Orchestrator, TickRequest


def _loop(
    name: str,
    *,
    cadence: int = 60,
    jobs: list[object] | None = None,
    error: Exception | None = None,
    always_on: bool = False,
) -> MiniLoop:
    def build(**_: object) -> list[object]:
        if error is not None:
            raise error
        return list(jobs or [])

    return MiniLoop(
        name=name,
        default_cadence_seconds=cadence,
        build_jobs=build,
        always_on=always_on,
    )


class OrchestratorTestCase(TestCase):
    def _orchestrator(
        self,
        *,
        loops: tuple[MiniLoop, ...],
        config: LoopsConfig | None = None,
        clock: dt.datetime | None = None,
        dispatched: list[str] | None = None,
    ) -> Orchestrator:
        fired_clock = clock or dt.datetime(2026, 5, 28, 12, tzinfo=dt.UTC)
        recorded = dispatched if dispatched is not None else []

        def _dispatch(jobs: list[object]) -> list[object]:
            # Test seam — record the jobs that would be dispatched.
            recorded.extend([f"job:{j}" for j in jobs])
            return list(jobs)

        return Orchestrator(
            config=config or LoopsConfig(),
            registry_fn=lambda: loops,
            clock=lambda: fired_clock,
            dispatch_fn=_dispatch,
        )

    def test_only_enabled_loops_dispatch(self) -> None:
        on = _loop("inbox", jobs=["a"])
        off = _loop("review", jobs=["b"])
        LoopState.objects.disable("review")
        recorded: list[str] = []
        orch = self._orchestrator(loops=(on, off), dispatched=recorded)
        report = orch.tick(TickRequest())
        assert "inbox" in report.dispatched_loops
        assert "review" in report.skipped_loops
        assert report.skipped_loops["review"] == "disabled"

    def test_cadence_gated_skip(self) -> None:
        # Mark inbox as fired 10s ago, but its cadence is 60s.
        clock = dt.datetime(2026, 5, 28, 12, tzinfo=dt.UTC)
        fired_at = clock - dt.timedelta(seconds=10)
        MiniLoopMarker.objects.mark_fired("inbox", fired_at)
        loop = _loop("inbox", cadence=60)
        orch = self._orchestrator(loops=(loop,), clock=clock)
        report = orch.tick(TickRequest())
        assert "inbox" not in report.dispatched_loops
        assert report.skipped_loops["inbox"] == "cadence"

    def test_cadence_elapsed_fires(self) -> None:
        clock = dt.datetime(2026, 5, 28, 12, tzinfo=dt.UTC)
        fired_at = clock - dt.timedelta(seconds=120)
        MiniLoopMarker.objects.mark_fired("inbox", fired_at)
        loop = _loop("inbox", cadence=60)
        orch = self._orchestrator(loops=(loop,), clock=clock)
        report = orch.tick(TickRequest())
        assert "inbox" in report.dispatched_loops

    def test_no_marker_fires_immediately(self) -> None:
        loop = _loop("inbox", cadence=60)
        orch = self._orchestrator(loops=(loop,))
        report = orch.tick(TickRequest())
        assert "inbox" in report.dispatched_loops
        # Marker recorded after the fire.
        assert MiniLoopMarker.objects.filter(name="inbox").exists()

    def test_marker_bumped_after_fire(self) -> None:
        clock = dt.datetime(2026, 5, 28, 12, tzinfo=dt.UTC)
        loop = _loop("inbox", cadence=60)
        orch = self._orchestrator(loops=(loop,), clock=clock)
        orch.tick(TickRequest())
        row = MiniLoopMarker.objects.get(name="inbox")
        assert row.last_fired_at == clock

    def test_error_in_one_loop_isolated(self) -> None:
        boom = _loop("inbox", error=RuntimeError("scanner exploded"))
        ok = _loop("review", jobs=["b"])
        orch = self._orchestrator(loops=(boom, ok))
        report = orch.tick(TickRequest())
        assert "inbox" in report.errors
        assert "scanner exploded" in report.errors["inbox"]
        assert "review" in report.dispatched_loops

    def test_always_on_runs_when_env_disabled_all(self) -> None:
        # The env kill-switch disables every non-always_on loop but never an
        # always_on one; only a DB hold can stop an always_on loop.
        old = os.environ.get("T3_LOOPS_DISABLED")
        try:
            os.environ["T3_LOOPS_DISABLED"] = "all"
            always = _loop("dispatch", always_on=True, jobs=["a"])
            normal = _loop("inbox", jobs=["b"])
            orch = self._orchestrator(loops=(always, normal))
            report = orch.tick(TickRequest())
            assert "dispatch" in report.dispatched_loops
            assert "inbox" not in report.dispatched_loops
        finally:
            if old is None:
                os.environ.pop("T3_LOOPS_DISABLED", None)
            else:
                os.environ["T3_LOOPS_DISABLED"] = old

    def test_t3_loops_disabled_env_honored(self) -> None:
        # TestCase doesn't get pytest's monkeypatch fixture; restore manually.
        old = os.environ.get("T3_LOOPS_DISABLED")
        try:
            os.environ["T3_LOOPS_DISABLED"] = "inbox"
            loop = _loop("inbox")
            other = _loop("review")
            orch = self._orchestrator(loops=(loop, other))
            report = orch.tick(TickRequest())
            assert "inbox" not in report.dispatched_loops
            assert "review" in report.dispatched_loops
        finally:
            if old is None:
                os.environ.pop("T3_LOOPS_DISABLED", None)
            else:
                os.environ["T3_LOOPS_DISABLED"] = old

    def test_deterministic_clock_injection(self) -> None:
        fixed = dt.datetime(2026, 5, 28, 12, tzinfo=dt.UTC)
        loop = _loop("inbox")
        orch = self._orchestrator(loops=(loop,), clock=fixed)
        report = orch.tick(TickRequest())
        assert report.started_at == fixed

    def test_silent_when_idle_no_summary(self) -> None:
        # 0 signals, 0 errors, policy="errors" → no DM emitted.
        sent: list[object] = []

        def _send(text: str, *, idempotency_key: str) -> None:
            sent.append((text, idempotency_key))

        loop = _loop("inbox", jobs=[])
        orch = self._orchestrator(loops=(loop,), config=LoopsConfig(summary_dm="errors"))
        orch._notify = _send  # type: ignore[method-assign]
        orch.tick(TickRequest())
        assert sent == []

    def test_summary_emits_when_errors(self) -> None:
        sent: list[tuple[str, str]] = []

        def _send(text: str, *, idempotency_key: str) -> None:
            sent.append((text, idempotency_key))

        boom = _loop("inbox", error=RuntimeError("nope"))
        orch = self._orchestrator(loops=(boom,), config=LoopsConfig(summary_dm="errors"))
        orch._notify = _send  # type: ignore[method-assign]
        orch.tick(TickRequest())
        assert len(sent) == 1
        text, key = sent[0]
        assert "inbox" in text
        assert "loops_tick_errors" in key

    def test_always_policy_two_same_day_ticks_get_distinct_keys(self) -> None:
        # policy=always must DM every tick; the idempotency key carries the tick
        # timestamp so the second same-day tick is not deduped away.
        sent: list[tuple[str, str]] = []

        def _send(text: str, *, idempotency_key: str) -> None:
            sent.append((text, idempotency_key))

        ticks = iter(
            [
                dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC),
                dt.datetime(2026, 5, 28, 12, 1, tzinfo=dt.UTC),
            ],
        )
        loop = _loop("inbox", jobs=[])
        orch = Orchestrator(
            config=LoopsConfig(summary_dm="always"),
            registry_fn=lambda: (loop,),
            clock=lambda: next(ticks),
            dispatch_fn=list,
        )
        orch._notify = _send  # type: ignore[method-assign]
        orch.tick(TickRequest())
        orch.tick(TickRequest())
        assert len(sent) == 2
        assert sent[0][1] != sent[1][1]
