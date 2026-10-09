"""Static checks for the standalone metered eval GitHub workflow."""

from pathlib import Path

_WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "eval.yml"


class TestMeteredEvalWorkflow:
    def test_backend_input_defaults_to_api(self) -> None:
        # #3222 exposes a `backend` dispatch input so a CLI-free lane can be selected,
        # but its DEFAULT is 'api' — the scheduled weekly run (empty input) is
        # unchanged, still the CLI-backed Agent SDK.
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert "backend:" in text
        assert 'default: "api"' in text

    def test_metered_command_threads_the_backend_input(self) -> None:
        # The eval command is no longer pinned to a literal `--backend api`; it threads
        # the selected backend, defaulting to 'api' for the scheduled run.
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert '--backend "$EVAL_BACKEND"' in text
        assert "EVAL_BACKEND: ${{ inputs.backend || 'api' }}" in text

    def test_claude_cli_install_is_gated_on_the_api_backend(self) -> None:
        # The `claude` CLI install/assert step must be SKIPPED for the CLI-free
        # anthropic_api backend (#3222) — a non-'api' backend spawns no `claude`
        # child. A scheduled run (empty input) still installs it (defaults to 'api').
        text = _WORKFLOW.read_text(encoding="utf-8")
        assert "if: ${{ (inputs.backend || 'api') == 'api' }}" in text
