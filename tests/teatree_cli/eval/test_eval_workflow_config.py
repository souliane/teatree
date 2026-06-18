"""Static checks for the standalone metered eval GitHub workflow."""

from pathlib import Path

_WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "eval.yml"


class TestMeteredEvalWorkflow:
    def test_manual_workflow_does_not_expose_a_backend_input_for_trials(self) -> None:
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert "backend:" not in text
        assert "inputs.backend" not in text
        assert "EVAL_BACKEND" not in text

    def test_metered_command_pins_sdk_backend(self) -> None:
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert '--backend "$EVAL_BACKEND"' not in text
        assert "--backend sdk" in text
