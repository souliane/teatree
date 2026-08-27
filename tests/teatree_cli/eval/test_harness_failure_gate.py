"""A ``production_hooks`` run that measured NOTHING gates every lane, on any surface.

``hooks_not_registered`` is the fail-loud for a hooked run whose shipped plugin never
registered — the lane silently degraded back to raw-model measurement. It used to be a
terminal :class:`~teatree.eval.models.EvalRun`, i.e. a failing VERDICT, so the
``surface: interactive`` exemption swallowed it: 6 of the 7 ``production_hooks``
scenarios are advisory, and the nightly ``clean_room`` shard contains only advisory ones
(souliane/teatree#3922).

These tests pin the separation. Every lane calls the unconditional
``RunGuards.hooks_registered`` guard BESIDE its verdict, so no surface exemption can
reach it; and the serialized ``advisory`` flag the ``eval-ci-heal`` combine job re-gates
on is never set for a row that measured nothing. The ordinary advisory exemption is
unchanged — ``tests/teatree_cli/eval/test_advisory_surface.py`` still owns that.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import typer

from teatree.cli.eval.escalate import escalate_failures
from teatree.cli.eval.multi_trial import TrialPolicy, collect_matrix_rows, run_model_matrix_lane, run_pass_at_k_lane
from teatree.cli.eval.run_modes import RunGuards
from teatree.cli.eval.single_trial import SingleTrialGates, run_single_trial
from teatree.eval.backends import API_BACKEND
from teatree.eval.green_proof import evaluate_green_proof
from teatree.eval.harness_failure import HOOKS_NOT_REGISTERED_REASON, measured_nothing
from teatree.eval.ladder import LadderPolicy, run_escalation_ladder
from teatree.eval.models import HEADLESS_SURFACE, INTERACTIVE_SURFACE, EvalRun, EvalSpec
from teatree.eval.pass_at_k import PassAtKResult, run_pass_at_k
from teatree.eval.report import ScenarioResult, evaluate
from teatree.eval.summary_json import render_summary_json

_GATES = SingleTrialGates(persist=False, baseline=False, gate_regressions=False, gate_cost_regression=False)


def _payload(results: list[ScenarioResult] | list[PassAtKResult]) -> dict[str, Any]:
    return json.loads(render_summary_json(results, head_sha="deadbeef", generated_at="2026-01-01T00:00:00Z"))


def _spec(name: str, *, surface: str = INTERACTIVE_SURFACE) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        surface=surface,
        production_hooks=True,
    )


def _run(name: str, *, reason: str) -> EvalRun:
    return EvalRun(
        spec_name=name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason=reason,
        is_error=True,
        raw_stdout="",
        raw_stderr="",
        # Non-zero so the unmetered-$0 guard never fires ahead of the one under test.
        cost_usd=0.01,
    )


def _result(
    name: str, *, surface: str = INTERACTIVE_SURFACE, reason: str = HOOKS_NOT_REGISTERED_REASON
) -> ScenarioResult:
    return evaluate(_spec(name, surface=surface), _run(name, reason=reason))


class _Runner:
    """A runner that returns one canned run per scenario."""

    def __init__(self, reason: str = HOOKS_NOT_REGISTERED_REASON) -> None:
        self._reason = reason

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name, reason=self._reason)


class TestTheAxisIsSeparateFromTheSurface:
    def test_the_hooks_reason_measures_nothing(self) -> None:
        assert measured_nothing(HOOKS_NOT_REGISTERED_REASON)

    def test_an_ordinary_failure_measured_something(self) -> None:
        # A graded FAIL has a verdict the surface exemption may legitimately weigh.
        assert not measured_nothing("success")


class TestTheGuardItself:
    def test_an_advisory_harness_failure_exits_non_zero(self) -> None:
        with pytest.raises(typer.Exit) as exc:
            RunGuards.hooks_registered([_result("chip")])
        assert exc.value.exit_code == 1

    def test_a_clean_run_is_a_no_op(self) -> None:
        RunGuards.hooks_registered([_result("chip", reason="success")])

    def test_the_message_names_the_scenario(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(typer.Exit):
            RunGuards.hooks_registered([_result("chip")])
        assert "chip" in capsys.readouterr().err


class TestEveryLaneGates:
    """The guard runs in every lane that drives the runner — advisory or not."""

    def test_single_trial_reds_on_an_advisory_harness_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", lambda *a, **k: _Runner())
        with pytest.raises(typer.Exit) as exc:
            run_single_trial(
                [_spec("chip")],
                backend="api",
                max_turns=None,
                transcript_dir=None,
                require_executed=False,
                max_budget_usd=1.0,
                effort=None,
                parallel=1,
                output_format="text",
                grader=None,
                judge=False,
                transcript_html=None,
                summary_md=None,
                gates=_GATES,
            )
        assert exc.value.exit_code == 1

    def test_pass_at_k_reds_on_an_advisory_harness_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _Runner())
        with pytest.raises(typer.Exit) as exc:
            run_pass_at_k_lane(
                [_spec("chip")],
                backend=API_BACKEND,
                max_turns=None,
                trials=2,
                require="any",
                output_format="text",
                persist=False,
                model_override="claude-sonnet-4-6",
            )
        assert exc.value.exit_code == 1

    def test_the_model_matrix_reds_on_an_advisory_harness_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _Runner())
        with pytest.raises(typer.Exit) as exc:
            run_model_matrix_lane(
                [_spec("chip")],
                backend=API_BACKEND,
                models="claude-sonnet-4-6",
                max_turns=None,
                trials=1,
                require="any",
                output_format="text",
                persist=False,
                baseline=False,
                gate_regressions=False,
            )
        assert exc.value.exit_code == 1


class TestTheMatrixRowCarriesTheSignal:
    """A multi-trial cell folds its trials' harness failure onto the row.

    A ``MatrixRow`` only carries the CAP terminal reason, so without an explicit flag a
    ``--trials k`` matrix cell loses the signal entirely and the guard reads clean.
    """

    def test_a_single_trial_cell_is_flagged(self) -> None:
        rows = collect_matrix_rows(
            [_spec("chip")], ["claude-sonnet-4-6"], runner=_Runner(), policy=TrialPolicy(trials=1)
        )
        assert rows[0].harness_failed is True

    def test_a_multi_trial_cell_is_flagged(self) -> None:
        rows = collect_matrix_rows(
            [_spec("chip")], ["claude-sonnet-4-6"], runner=_Runner(), policy=TrialPolicy(trials=2)
        )
        assert rows[0].harness_failed is True

    def test_a_clean_cell_is_not_flagged(self) -> None:
        rows = collect_matrix_rows(
            [_spec("chip")], ["claude-sonnet-4-6"], runner=_Runner(reason="success"), policy=TrialPolicy(trials=2)
        )
        assert rows[0].harness_failed is False

    def test_a_ladder_cell_is_flagged(self) -> None:
        # The ladder folds its own cells rather than going through `_matrix_trial`, so a
        # fold that drops the flag makes `ladder.ladder`'s guard call VACUOUS — it reads
        # a clean row and the lane never gates, which is #3922 again on that lane alone.
        rows = run_escalation_ladder(
            [_spec("chip")],
            ["claude-sonnet-4-6"],
            run_trial=lambda spec: _result(spec.name),
            policy=LadderPolicy(trials=1, require="all"),
        )
        assert rows[0].harness_failed is True

    def test_a_clean_ladder_cell_is_not_flagged(self) -> None:
        rows = run_escalation_ladder(
            [_spec("chip")],
            ["claude-sonnet-4-6"],
            run_trial=lambda spec: _result(spec.name, reason="success"),
            policy=LadderPolicy(trials=1, require="all"),
        )
        assert rows[0].harness_failed is False


class TestTheAggregateFold:
    """``PassAtKResult.harness_failed`` folds the way ``is_error`` does."""

    def _aggregate(self, *trials: ScenarioResult) -> PassAtKResult:
        remaining = list(trials)
        return run_pass_at_k(_spec("chip"), lambda _spec: remaining.pop(0), k=len(trials), require="any")

    def test_every_executed_trial_failing_the_harness_folds_true(self) -> None:
        assert self._aggregate(_result("chip"), _result("chip")).harness_failed is True

    def test_one_trial_that_genuinely_ran_folds_false(self) -> None:
        # That trial proves the wiring came up, so `require="any"` may still grade the cell.
        assert self._aggregate(_result("chip"), _result("chip", reason="success")).harness_failed is False

    def test_an_all_skipped_cell_folds_false(self) -> None:
        skipped = ScenarioResult(spec=_spec("chip"), run=_run("chip", reason="skip"), matcher_results=(), skipped=True)
        assert self._aggregate(skipped).harness_failed is False


class TestTheSerializedAdvisoryFlag:
    """The merged-artifact gate re-runs after the shard exits; the flag must agree."""

    def test_an_interactive_harness_failure_is_not_written_advisory(self) -> None:
        payload = _payload([_result("chip")])
        assert payload["scenarios"][0]["advisory"] is False

    def test_a_pass_at_k_row_carries_the_harness_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A pass@k aggregate carries only the CAP reason, so without the harness fold the
        # serialized row would report an empty reason and read advisory.
        monkeypatch.setattr("teatree.eval.summary_json.find_spec", lambda _name: _spec("chip"))
        aggregate = run_pass_at_k(_spec("chip"), lambda spec: _result(spec.name), k=2, require="any")
        row = _payload([aggregate])["scenarios"][0]
        assert row["terminal_reason"] == HOOKS_NOT_REGISTERED_REASON
        assert row["advisory"] is False

    def test_an_ordinary_interactive_failure_stays_advisory(self) -> None:
        payload = _payload([_result("chip", reason="success")])
        assert payload["scenarios"][0]["advisory"] is True

    # `expected_total` is main's #4228 collection-completeness term: a proof is only
    # green if the run COVERED the catalog. Both payloads below define exactly one
    # scenario, so 1 is the value that leaves the harness-failure axis as the only
    # thing under test — at 0 or >1 the coverage arm reds both cases and the second
    # assertion would pass for a reason that has nothing to do with #3922.
    def test_the_green_proof_reds_on_a_harness_failure(self) -> None:
        assert not evaluate_green_proof(_payload([_result("chip")]), expected_total=1).is_green

    def test_the_green_proof_still_exempts_an_ordinary_interactive_red(self) -> None:
        assert evaluate_green_proof(_payload([_result("chip", reason="success")]), expected_total=1).is_green


class TestTheEscalationVerdict:
    """``--escalate-on-fail`` re-runs a failure; a confirmed harness failure is hard red."""

    def test_a_confirmed_advisory_harness_failure_is_hard_red(self) -> None:
        report = escalate_failures([_result("chip")], lambda spec: _result(spec.name), escalate_trials=2)
        assert report.hard_red

    def test_a_confirmed_ordinary_advisory_failure_is_not_hard_red(self) -> None:
        results = [_result("chip", reason="success")]
        report = escalate_failures(results, lambda spec: _result(spec.name, reason="success"), escalate_trials=2)
        assert not report.hard_red


class TestHeadlessIsUnchanged:
    """The guard is surface-blind: a headless harness failure gated before and still does."""

    def test_a_headless_harness_failure_also_exits(self) -> None:
        with pytest.raises(typer.Exit):
            RunGuards.hooks_registered([_result("slack", surface=HEADLESS_SURFACE)])
