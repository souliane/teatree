"""Bounded concurrent driver for the sequential claude -p eval runner."""

import sqlite3
import threading
import time
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.eval.models import EvalRun, EvalSpec, Matcher
from teatree.eval.parallel import DEFAULT_PARALLEL, ConcurrencyGovernor, run_specs
from teatree.llm.anthropic_limits import CreditExhaustedError


def _spec(tmp_path: Path, *, name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path=str(tmp_path / "agent.md"),
        prompt="p",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=tmp_path / "spec.yaml",
    )


class _RecordingRunner:
    """A fake runner that records concurrency and echoes the spec name."""

    def __init__(self, *, work_seconds: float = 0.0) -> None:
        self._work_seconds = work_seconds
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.ran: list[str] = []

    def run(self, spec: EvalSpec) -> EvalRun:
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self._work_seconds:
                time.sleep(self._work_seconds)
            with self._lock:
                self.ran.append(spec.name)
            return EvalRun(
                spec_name=spec.name,
                tool_calls=(),
                text_blocks=(),
                terminal_reason="success",
                is_error=False,
                raw_stdout="",
                raw_stderr="",
            )
        finally:
            with self._lock:
                self.in_flight -= 1


class TestRunSpecs:
    def test_default_is_serial(self) -> None:
        assert DEFAULT_PARALLEL == 1

    def test_results_preserve_spec_order(self, tmp_path: Path) -> None:
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(6)]
        runner = _RecordingRunner(work_seconds=0.01)
        runs = run_specs(runner, specs, parallel=4)
        assert [r.spec_name for r in runs] == [s.name for s in specs]

    def test_concurrency_cap_is_respected(self, tmp_path: Path) -> None:
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(8)]
        runner = _RecordingRunner(work_seconds=0.05)
        run_specs(runner, specs, parallel=3)
        assert runner.peak_in_flight <= 3

    def test_parallel_actually_overlaps(self, tmp_path: Path) -> None:
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(4)]
        runner = _RecordingRunner(work_seconds=0.05)
        run_specs(runner, specs, parallel=4)
        # With 4 workers and 4 slow specs the pool must overlap at least 2 at once.
        assert runner.peak_in_flight >= 2

    def test_parallel_one_runs_strictly_sequential(self, tmp_path: Path) -> None:
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(5)]
        runner = _RecordingRunner(work_seconds=0.01)
        runs = run_specs(runner, specs, parallel=1)
        assert runner.peak_in_flight == 1
        assert [r.spec_name for r in runs] == [s.name for s in specs]

    def test_empty_specs_yields_empty(self) -> None:
        assert run_specs(_RecordingRunner(), [], parallel=4) == []

    def test_parallel_clamped_to_spec_count(self, tmp_path: Path) -> None:
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(2)]
        runner = _RecordingRunner(work_seconds=0.02)
        run_specs(runner, specs, parallel=99)
        assert runner.peak_in_flight <= 2

    def test_one_spec_error_does_not_drop_the_others(self, tmp_path: Path) -> None:
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(4)]

        class _OneRaises:
            def run(self, spec: EvalSpec) -> EvalRun:
                if spec.name == "s2":
                    msg = "boom"
                    raise RuntimeError(msg)
                return EvalRun(
                    spec_name=spec.name,
                    tool_calls=(),
                    text_blocks=(),
                    terminal_reason="success",
                    is_error=False,
                    raw_stdout="",
                    raw_stderr="",
                )

        runs = run_specs(_OneRaises(), specs, parallel=4)
        assert [r.spec_name for r in runs] == [s.name for s in specs]
        errored = next(r for r in runs if r.spec_name == "s2")
        assert errored.is_error
        assert "boom" in errored.terminal_reason or "RuntimeError" in errored.terminal_reason
        assert all(not r.is_error for r in runs if r.spec_name != "s2")


class _CreditExhaustedRunner:
    """A runner whose every ``run`` raises ``CreditExhaustedError`` (a $0 metered key).

    Records how many runs it serviced so a test can prove the suite ABORTED early
    rather than redding every scenario on the dead key.
    """

    def __init__(self, *, work_seconds: float = 0.0) -> None:
        self._work_seconds = work_seconds
        self._lock = threading.Lock()
        self.ran = 0

    def run(self, spec: EvalSpec) -> EvalRun:
        with self._lock:
            self.ran += 1
        if self._work_seconds:
            time.sleep(self._work_seconds)
        msg = "API credits exhausted — add credits at console.anthropic.com"
        raise CreditExhaustedError(msg)


class TestCreditExhaustedAbortsTheSuite:
    """A $0 metered key is terminal for the WHOLE suite — it must NOT become N reds.

    On the UNFIXED code ``_safe_run``'s broad ``except Exception`` swallowed the
    ``CreditExhaustedError`` into a per-scenario errored ``EvalRun``, so the batch
    kept running against the dead key and red'd every remaining scenario
    identically. These assert the opposite: the error PROPAGATES (aborts) and
    fewer than all scenarios run.
    """

    def test_serial_credit_exhaustion_aborts_and_does_not_red_every_scenario(self, tmp_path: Path) -> None:
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(5)]
        runner = _CreditExhaustedRunner()
        with pytest.raises(CreditExhaustedError, match=r"console\.anthropic\.com"):
            run_specs(runner, specs, parallel=1)
        # Aborted on the FIRST scenario — never red'd the remaining four.
        assert runner.ran == 1

    def test_parallel_credit_exhaustion_aborts_and_cancels_pending_scenarios(self, tmp_path: Path) -> None:
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(8)]
        # A small per-run delay so the pool's first wave hits the dead key and the
        # abort cancels the not-yet-started futures before they are dispatched.
        runner = _CreditExhaustedRunner(work_seconds=0.02)
        with pytest.raises(CreditExhaustedError):
            run_specs(runner, specs, parallel=4)
        # Pending scenarios were cancelled, so NOT all eight ran on the dead key.
        assert runner.ran < len(specs)


def _completed(name: str, *, throttle_retries: int = 0, is_error: bool = False) -> EvalRun:
    return EvalRun(
        spec_name=name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="throttled" if is_error else "success",
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
        throttle_retries=throttle_retries,
    )


class TestConcurrencyGovernor:
    """Layer 3: an AIMD governor backs the shared permit count off a throttle.

    Standard congestion control: a throttle event (a run that rode out one or more
    Layer-2 retries) multiplicatively HALVES the permit ceiling toward a floor of 1;
    N consecutive clean completions ADDITIVELY grow it one step back toward the
    worker count. A cooldown keeps a burst of near-simultaneous throttles from
    collapsing the ceiling in one step.
    """

    def test_starts_at_the_worker_count(self) -> None:
        assert ConcurrencyGovernor(8).limit == 8

    def test_throttle_multiplicatively_halves_toward_a_floor_of_one(self) -> None:
        gov = ConcurrencyGovernor(8, cooldown=0.0)
        gov.record_completion(_completed("a", throttle_retries=1))
        assert gov.limit == 4
        gov.record_completion(_completed("b", throttle_retries=2))
        assert gov.limit == 2
        gov.record_completion(_completed("c", throttle_retries=1))
        assert gov.limit == 1
        gov.record_completion(_completed("d", throttle_retries=1))
        assert gov.limit == 1  # floored at 1, never 0

    def test_clean_completions_additively_grow_back_toward_workers(self) -> None:
        gov = ConcurrencyGovernor(8, cooldown=0.0, grow_after=3)
        gov.record_completion(_completed("a", throttle_retries=1))  # 8 -> 4
        assert gov.limit == 4
        for i in range(2):
            gov.record_completion(_completed(f"c{i}"))
        assert gov.limit == 4  # not yet N clean completions
        gov.record_completion(_completed("c2"))  # third clean -> +1
        assert gov.limit == 5

    def test_a_clean_run_never_grows_past_the_worker_count(self) -> None:
        gov = ConcurrencyGovernor(2, grow_after=1)
        for i in range(5):
            gov.record_completion(_completed(f"c{i}"))
        assert gov.limit == 2

    def test_cooldown_suppresses_a_second_immediate_shrink(self) -> None:
        clock = {"t": 0.0}
        gov = ConcurrencyGovernor(8, cooldown=5.0, clock=lambda: clock["t"])
        gov.record_completion(_completed("a", throttle_retries=1))  # 8 -> 4
        assert gov.limit == 4
        gov.record_completion(_completed("b", throttle_retries=1))  # within cooldown -> ignored
        assert gov.limit == 4
        clock["t"] = 10.0
        gov.record_completion(_completed("c", throttle_retries=1))  # cooldown elapsed -> 4 -> 2
        assert gov.limit == 2

    def test_a_non_throttle_error_neither_shrinks_nor_grows(self) -> None:
        gov = ConcurrencyGovernor(8, cooldown=0.0, grow_after=1)
        gov.record_completion(_completed("a", throttle_retries=1))  # 8 -> 4
        assert gov.limit == 4
        gov.record_completion(_completed("crash", is_error=True))  # a genuine crash is neutral
        assert gov.limit == 4


class TestRunSpecsGovernorIntegration:
    def test_run_specs_drains_every_spec_even_when_every_run_throttles(self, tmp_path: Path) -> None:
        # Every run reports a throttle, so the governor shrinks toward its floor.
        # The floor of 1 keeps progress, so all specs still complete in order — the
        # governed path must never deadlock nor drop a spec.
        specs = [_spec(tmp_path, name=f"s{i}") for i in range(6)]

        class _AlwaysThrottled:
            def run(self, spec: EvalSpec) -> EvalRun:
                return _completed(spec.name, throttle_retries=1)

        runs = run_specs(_AlwaysThrottled(), specs, parallel=4)
        assert [r.spec_name for r in runs] == [s.name for s in specs]


class TestPoolWorkerConnectionHygiene(TestCase):
    """A pool worker that touches the ORM must not strand its raw DB handle.

    A real runner resolves its model tier and effective settings from the
    ``ConfigSetting`` store, so each worker opens its OWN thread-local Django
    connection. ``close()`` is a documented no-op on the in-memory test database,
    so the raw DB-API handle is closed directly — otherwise it is finalized at a
    later GC as a ``ResourceWarning: unclosed database`` charged to an unrelated
    test, and is a real connection leak in production.
    """

    def test_workers_leave_no_open_handle(self) -> None:
        specs = [
            EvalSpec(
                name=f"s{i}",
                scenario="s",
                agent_path="agent.md",
                prompt="p",
                matchers=(),
                source_path=Path("spec.yaml"),
            )
            for i in range(3)
        ]
        raws: list[sqlite3.Connection] = []
        lock = threading.Lock()

        class _OrmTouchingRunner:
            def run(self, spec: EvalSpec) -> EvalRun:
                from django.db import connection  # noqa: PLC0415 — the WORKER thread's connection

                connection.ensure_connection()
                with lock:
                    raws.append(connection.connection)
                return _completed(spec.name)

        runs = run_specs(_OrmTouchingRunner(), specs, parallel=3)

        assert [r.spec_name for r in runs] == [s.name for s in specs]
        assert len(raws) == 3, f"the runner never opened a connection per worker: {raws}"
        for raw in raws:
            with pytest.raises(sqlite3.ProgrammingError):
                raw.execute("SELECT 1")
