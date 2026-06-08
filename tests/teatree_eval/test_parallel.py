"""Bounded concurrent driver for the sequential claude -p eval runner."""

import threading
import time
from pathlib import Path

from teatree.eval.models import EvalRun, EvalSpec, Matcher
from teatree.eval.parallel import DEFAULT_PARALLEL, run_specs


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
