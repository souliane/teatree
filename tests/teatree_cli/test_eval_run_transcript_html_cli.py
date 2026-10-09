"""``t3 eval run --transcript-html`` end-to-end through the CLI.

Integration: drive the real ``t3 eval run`` typer command (the api runner stubbed
so no metered call is made) with ``--trials 3 --transcript-html <path>`` and
``T3_EVAL_IN_CONTAINER=1`` (in-process, no docker re-route), then assert the
per-trial transcript artifact landed on disk with the agent's transcript in it —
the durable evidence a maintainer opens to triage a red metered lane.
"""

from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall
from teatree.llm.credentials import AnthropicSubscriptionCredential


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        model="claude-sonnet-4-6",
    )


def _stub_runner_class() -> type:
    class _StubRunner:
        def __init__(self, *_: object, **__: object) -> None: ...

        def run(self, spec: EvalSpec) -> EvalRun:
            return EvalRun(
                spec_name=spec.name,
                tool_calls=(EvalToolCall(name="Bash", input={"command": f"echo {spec.name}"}, turn=1),),
                text_blocks=(f"reasoning for {spec.name}",),
                terminal_reason="success",
                is_error=False,
                raw_stdout="",
                raw_stderr="",
                cost_usd=0.02,
            )

    return _StubRunner


class TestRunEmitsTranscriptArtifact(TestCase):
    def test_trials_run_writes_the_per_trial_transcript_to_the_path(self) -> None:
        out = Path(self._artifact_dir()) / "eval-transcripts.html"
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha")]),
            patch("teatree.eval.backends.ApiInProcessRunner", _stub_runner_class()),
            patch.object(AnthropicSubscriptionCredential, "export", return_value="oauth-test"),
        ):
            result = CliRunner().invoke(
                app,
                ["eval", "run", "--backend", "api", "--trials", "3", "--transcript-html", str(out)],
                env={"T3_EVAL_IN_CONTAINER": "1"},
            )
        assert "Traceback" not in result.output, result.output
        assert out.is_file()
        body = out.read_text(encoding="utf-8")
        assert "alpha" in body
        assert "reasoning for alpha" in body  # the per-trial transcript IS in the artifact
        assert "echo alpha" in body

    def test_models_matrix_rejects_transcript_html(self) -> None:
        out = Path(self._artifact_dir()) / "report.html"
        with patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha")]):
            result = CliRunner().invoke(
                app,
                ["eval", "run", "--backend", "api", "--models", "opus", "--transcript-html", str(out)],
                env={"T3_EVAL_IN_CONTAINER": "1"},
            )
        assert result.exit_code == 2
        assert "--transcript-html" in result.output
        assert not out.exists()

    @staticmethod
    def _artifact_dir() -> str:
        import tempfile  # noqa: PLC0415

        return tempfile.mkdtemp()
