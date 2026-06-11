"""Per-cell error isolation in the matrix/benchmark loop (#2192).

A multi-cell benchmark must never let one cell's transient runner exception
abort the whole run: an unexpected error is retried a bounded number of times,
then recorded as an ERRORED row (distinct from a graded FAIL) so the full
comparison table is still produced. These tests drive ``collect_matrix_rows``
with a fake runner — never the real SDK — so the resilience contract is pinned
deterministically.
"""

from pathlib import Path

import pytest
import typer

from teatree.cli.eval.multi_trial import collect_matrix_rows
from teatree.eval.models import EvalRun, EvalSpec


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        judge=None,
    )


def _clean_run(spec: EvalSpec) -> EvalRun:
    """A matcher-less, non-error, non-skip run — grades to PASS."""
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="end_turn",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.01,
    )


class _RaiseOnCellRunner:
    """Raises on one (scenario, model) cell; returns a clean run everywhere else."""

    def __init__(self, *, fail_scenario: str, fail_model: str) -> None:
        self._fail_scenario = fail_scenario
        self._fail_model = fail_model

    def run(self, spec: EvalSpec) -> EvalRun:
        if spec.name == self._fail_scenario and spec.model == self._fail_model:
            msg = "Claude Code returned an error result: success"
            raise Exception(msg)  # noqa: TRY002 — mirrors the SDK's bare-Exception transient
        return _clean_run(spec)


class _RaiseNTimesRunner:
    """Raises the first ``fails`` attempts on the target cell, then returns clean."""

    def __init__(self, *, fail_scenario: str, fail_model: str, fails: int) -> None:
        self._fail_scenario = fail_scenario
        self._fail_model = fail_model
        self._remaining = fails

    def run(self, spec: EvalSpec) -> EvalRun:
        if spec.name == self._fail_scenario and spec.model == self._fail_model and self._remaining > 0:
            self._remaining -= 1
            msg = "transient blip"
            raise Exception(msg)  # noqa: TRY002 — mirrors the SDK's bare-Exception transient
        return _clean_run(spec)


class _AlwaysRaisesRunner:
    """Always raises on the target cell, counting attempts; clean elsewhere."""

    def __init__(self, *, fail_scenario: str, fail_model: str) -> None:
        self._fail_scenario = fail_scenario
        self._fail_model = fail_model
        self.attempts = 0

    def run(self, spec: EvalSpec) -> EvalRun:
        if spec.name == self._fail_scenario and spec.model == self._fail_model:
            self.attempts += 1
            msg = "permanently broken cell"
            raise Exception(msg)  # noqa: TRY002 — mirrors the SDK's bare-Exception transient
        return _clean_run(spec)


class _RaisesTyperExitRunner:
    """Raises ``typer.Exit`` — a control-flow signal that must NOT be isolated."""

    def run(self, spec: EvalSpec) -> EvalRun:
        raise typer.Exit(code=2)


class _RaisesBaseExceptionRunner:
    """Raises a given ``BaseException`` (e.g. KeyboardInterrupt/SystemExit)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def run(self, spec: EvalSpec) -> EvalRun:
        raise self._exc


class _CappedTrialRunner:
    """One trial of a target cell hits a cap (max_turns); the rest finish clean.

    Models the REAL multi-trial CI cell where a partial trajectory satisfied the
    (matcher-less) grade but the run never finished within the cap. The capped
    trial must taint the whole cell's gate verdict.
    """

    def __init__(self, *, capped_scenario: str, capped_model: str, cap_on_attempt: int) -> None:
        self._capped_scenario = capped_scenario
        self._capped_model = capped_model
        self._cap_on_attempt = cap_on_attempt
        self._attempts: dict[tuple[str, str], int] = {}

    def run(self, spec: EvalSpec) -> EvalRun:
        key = (spec.name, spec.model)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        if (
            spec.name == self._capped_scenario
            and spec.model == self._capped_model
            and self._attempts[key] == self._cap_on_attempt
        ):
            return EvalRun(
                spec_name=spec.name,
                tool_calls=(),
                text_blocks=(),
                terminal_reason="max_turns",
                is_error=False,
                raw_stdout="",
                raw_stderr="",
                cost_usd=0.01,
            )
        return _clean_run(spec)


def _by_key(rows: list, scenario: str, model: str):
    return next(r for r in rows if r.scenario == scenario and r.model == model)


class TestPerCellErrorIsolation:
    def test_one_failing_cell_does_not_abort_the_others(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        runner = _RaiseOnCellRunner(fail_scenario="alpha", fail_model="haiku")
        rows = collect_matrix_rows(specs, ["opus", "haiku"], runner=runner, trials=1, require="any")

        # All four cells produced a row — nothing was lost.
        assert len(rows) == 4
        failing = _by_key(rows, "alpha", "haiku")
        assert failing.errored is True
        assert failing.passed is False
        assert failing.skipped is False
        # Every other cell graded normally (clean run -> pass).
        for scenario, model in [("alpha", "opus"), ("beta", "opus"), ("beta", "haiku")]:
            other = _by_key(rows, scenario, model)
            assert other.errored is False
            assert other.passed is True

    def test_retry_then_succeed_is_graded_not_errored(self) -> None:
        specs = [_spec("alpha")]
        # Fails twice, succeeds on the third attempt — within the bounded retries.
        runner = _RaiseNTimesRunner(fail_scenario="alpha", fail_model="opus", fails=2)
        rows = collect_matrix_rows(specs, ["opus"], runner=runner, trials=1, require="any")
        (row,) = rows
        assert row.errored is False
        assert row.passed is True

    def test_always_raising_cell_errors_after_three_attempts(self) -> None:
        specs = [_spec("alpha")]
        runner = _AlwaysRaisesRunner(fail_scenario="alpha", fail_model="opus")
        rows = collect_matrix_rows(specs, ["opus"], runner=runner, trials=1, require="any")
        (row,) = rows
        assert row.errored is True
        assert row.passed is False
        # Bounded give-up: 3 attempts total (1 + 2 retries), not an infinite loop.
        assert runner.attempts == 3

    def test_typer_exit_is_not_swallowed_as_an_errored_cell(self) -> None:
        # typer.Exit subclasses RuntimeError (so a bare `except Exception` would
        # catch it) — but it is a control-flow signal (e.g. a parse error), not a
        # transient cell failure. It must propagate, not be retried/ERRORED.
        specs = [_spec("alpha")]
        with pytest.raises(typer.Exit):
            collect_matrix_rows(specs, ["opus"], runner=_RaisesTyperExitRunner(), trials=1, require="any")

    @pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])
    def test_base_exception_propagates_through_the_resilient_wrapper(self, exc: BaseException) -> None:
        # KeyboardInterrupt/SystemExit are BaseExceptions, not Exceptions, so the
        # `except Exception` cell-isolation never catches them — they propagate
        # (a Ctrl-C must abort the whole run, never be retried/ERRORED).
        specs = [_spec("alpha")]
        with pytest.raises(type(exc)):
            collect_matrix_rows(specs, ["opus"], runner=_RaisesBaseExceptionRunner(exc), trials=1, require="any")


class TestMatrixCellCapTaint:
    def test_multi_trial_cell_with_a_capped_trial_is_not_a_pass(self) -> None:
        # A multi-trial (`--trials 3 --require any`) matrix cell whose aggregate is
        # cap-tainted (one trial hit max_turns) must NOT grade as a passing cell —
        # `passed` flips to False and `terminal_reason` carries the cap reason. A
        # clean trial cannot prop up a green matrix cell over a capped sibling.
        specs = [_spec("alpha")]
        runner = _CappedTrialRunner(capped_scenario="alpha", capped_model="opus", cap_on_attempt=2)
        rows = collect_matrix_rows(specs, ["opus"], runner=runner, trials=3, require="any")
        (row,) = rows
        assert row.errored is False
        assert row.skipped is False
        assert row.terminal_reason == "max_turns"
        assert row.passed is False  # cap-tainted aggregate is not a passing cell
