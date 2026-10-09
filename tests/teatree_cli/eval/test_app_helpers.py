"""``t3 eval run`` argument validators (:mod:`teatree.cli.eval.app_helpers`).

The fresh-run-only validator gates ``--trials`` / ``--models`` on the metered
``api`` backend: a multi-trial or matrix run RUNS the model, so it must opt into
the metered/api lane explicitly rather than silently grading a stored transcript.
"""

from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.eval.app_helpers import (
    reject_multi_trial_with_model_override,
    require_api_backend_for_fresh_run,
    require_metering_backend_for_cost_bounds,
    resolve_escalation,
)
from teatree.cli.eval.single_trial import EscalationConfig
from teatree.eval.backends import (
    ANTHROPIC_API_BACKEND,
    API_BACKEND,
    PYDANTIC_AI_BACKEND,
    TRANSCRIPT_BACKEND,
    UNMETERED_FRESH_BACKENDS,
)


class TestRequireApiBackendForFreshRun:
    def test_single_trial_transcript_run_is_allowed(self) -> None:
        # A plain single-trial transcript run never RUNS a model, so it needs no api opt-in.
        require_api_backend_for_fresh_run(backend=TRANSCRIPT_BACKEND, trials=1, models=None)

    def test_trials_on_the_api_backend_is_allowed(self) -> None:
        require_api_backend_for_fresh_run(backend=API_BACKEND, trials=3, models=None)

    def test_models_matrix_on_the_api_backend_is_allowed(self) -> None:
        require_api_backend_for_fresh_run(backend=API_BACKEND, trials=1, models="opus,sonnet")

    def test_trials_on_the_transcript_backend_is_rejected(self) -> None:
        # A multi-trial run RUNS the model k times, so it must opt into the metered
        # api lane — grading a single stored transcript k times is meaningless.
        with pytest.raises(typer.Exit) as exc:
            require_api_backend_for_fresh_run(backend=TRANSCRIPT_BACKEND, trials=3, models=None)
        assert exc.value.exit_code == 2

    def test_models_on_the_transcript_backend_is_rejected(self) -> None:
        with pytest.raises(typer.Exit) as exc:
            require_api_backend_for_fresh_run(backend=TRANSCRIPT_BACKEND, trials=1, models="opus,sonnet")
        assert exc.value.exit_code == 2

    def test_rejection_message_names_the_api_backend_fix(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The fix the user is told to apply must be `--backend api`, never the old token.
        with pytest.raises(typer.Exit):
            require_api_backend_for_fresh_run(backend=TRANSCRIPT_BACKEND, trials=2, models=None)
        err = capsys.readouterr().err
        assert "--backend api" in err
        assert "--backend 'sdk'" not in err


class TestRequireMeteringBackendForCostBounds:
    """``--gate-cost-bounds`` on a backend that records no cost is UNSATISFIABLE, both ways.

    Every backend in ``UNMETERED_FRESH_BACKENDS`` drives the model through
    ``PydanticAiRunner``, which reports no ``cost_usd``, so a pinned ceiling reads
    ``MISSING`` on a run that executed perfectly and an unpinned set reads VACUOUS.
    Calibrating a ceiling makes it WORSE. That is an operator error, and it must exit
    loud at the CLI boundary — never pass silently (the skip-as-pass bug this branch
    removes) and never emit per-scenario violations that read as real cost regressions.
    """

    @pytest.mark.parametrize("backend", UNMETERED_FRESH_BACKENDS)
    def test_unmetered_backend_with_the_gate_is_rejected(self, backend: str) -> None:
        with pytest.raises(typer.Exit) as exc:
            require_metering_backend_for_cost_bounds(backend=backend, gate_cost_bounds=True)
        assert exc.value.exit_code == 2

    @pytest.mark.parametrize("backend", [ANTHROPIC_API_BACKEND, PYDANTIC_AI_BACKEND])
    def test_rejection_names_the_backend_and_the_metering_requirement(
        self, backend: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(typer.Exit):
            require_metering_backend_for_cost_bounds(backend=backend, gate_cost_bounds=True)
        err = capsys.readouterr().err
        assert backend in err
        assert "--gate-cost-bounds" in err
        assert f"--backend {API_BACKEND}" in err

    @pytest.mark.parametrize("backend", [API_BACKEND, TRANSCRIPT_BACKEND])
    def test_cost_recording_backends_keep_the_gate(self, backend: str) -> None:
        require_metering_backend_for_cost_bounds(backend=backend, gate_cost_bounds=True)

    @pytest.mark.parametrize("backend", UNMETERED_FRESH_BACKENDS)
    def test_unmetered_backend_without_the_gate_is_allowed(self, backend: str) -> None:
        # The backend is fine; only ASKING it for a cost verdict it cannot give is not.
        require_metering_backend_for_cost_bounds(backend=backend, gate_cost_bounds=False)


class TestCostBoundsRefusalReachesTheCli:
    """The helper existing is half of it — ``t3 eval run`` has to call it BEFORE dispatching."""

    def _invoke(self, argv: list[str]) -> tuple[int, str]:
        from teatree.cli import app  # noqa: PLC0415 — the CLI app is expensive to import at module scope.

        with patch("teatree.cli.eval.app.dispatch_resolved_run") as dispatch:
            result = CliRunner().invoke(app, argv)
        assert not dispatch.called, "the unsatisfiable gate still dispatched a run"
        return result.exit_code, result.output

    def test_the_ci_lane_flag_combination_exits_two_without_running(self) -> None:
        # The exact `.eval-suite` shape: the CI backend never reports cost, so this
        # combination could only ever produce MISSING/VACUOUS red.
        exit_code, output = self._invoke(
            ["eval", "run", "--backend", ANTHROPIC_API_BACKEND, "--local", "--gate-cost-bounds"]
        )
        assert exit_code == 2, output
        assert ANTHROPIC_API_BACKEND in output


class TestResolveEscalation:
    def test_off_returns_none(self) -> None:
        assert resolve_escalation(escalate_on_fail=False, escalate_trials=3, trials=1, models=None) is None

    def test_on_single_trial_returns_config(self) -> None:
        config = resolve_escalation(escalate_on_fail=True, escalate_trials=4, trials=1, models=None)
        assert config == EscalationConfig(escalate_trials=4)

    def test_rejected_on_multi_trial(self) -> None:
        # --trials>1 already aggregates across trials — escalating it would double-count.
        with pytest.raises(typer.Exit) as exc:
            resolve_escalation(escalate_on_fail=True, escalate_trials=3, trials=3, models=None)
        assert exc.value.exit_code == 2

    def test_rejected_on_models_matrix(self) -> None:
        with pytest.raises(typer.Exit) as exc:
            resolve_escalation(escalate_on_fail=True, escalate_trials=3, trials=1, models="opus,sonnet")
        assert exc.value.exit_code == 2

    def test_rejected_when_escalate_trials_below_two(self) -> None:
        with pytest.raises(typer.Exit) as exc:
            resolve_escalation(escalate_on_fail=True, escalate_trials=1, trials=1, models=None)
        assert exc.value.exit_code == 2


class TestModelOverrideCannotSilentlyDropTrials:
    """``--model`` routes to the single-trial runner, so ``--trials``/``--require`` were dropped.

    The operator asked for k trials aggregated by ``--require`` and got one trial with
    no error — a pass@k gate that never ran k times.
    """

    def test_model_with_multiple_trials_is_refused(self) -> None:
        with pytest.raises(typer.Exit) as exc:
            reject_multi_trial_with_model_override(model="opus", trials=5)
        assert exc.value.exit_code == 2

    def test_model_on_a_single_trial_is_allowed(self) -> None:
        reject_multi_trial_with_model_override(model="opus", trials=1)

    def test_trials_without_a_model_override_is_allowed(self) -> None:
        reject_multi_trial_with_model_override(model=None, trials=5)
