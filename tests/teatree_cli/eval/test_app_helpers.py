"""``t3 eval run`` argument validators (:mod:`teatree.cli.eval.app_helpers`).

The fresh-run-only validator gates ``--trials`` / ``--models`` on the metered
``api`` backend: a multi-trial or matrix run RUNS the model, so it must opt into
the metered/api lane explicitly rather than silently grading a stored transcript.
"""

import pytest
import typer

from teatree.cli.eval.app_helpers import require_api_backend_for_fresh_run, resolve_escalation
from teatree.cli.eval.single_trial import EscalationConfig
from teatree.eval.backends import API_BACKEND, TRANSCRIPT_BACKEND


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
