"""``t3 eval run --trials k --summary-md <path>`` — the sanitized aggregate dashboard.

The weekly metered lane and the selective-PR lane both pass ``--summary-md`` so
each shard emits a publish-safe markdown dashboard (counts + a
``scenario | lane | verdict | trials`` table) alongside the PRIVATE
``--transcript-html``. These pin that the multi-trial path writes the file from
THIS run's results (no re-run), keeps the transcript OUT of it, and still writes
it when the lane is about to exit non-zero.
"""

from pathlib import Path

import pytest

from teatree.cli.eval.multi_trial import run_pass_at_k_lane
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher

SENTINEL = "SECRET_TRANSCRIPT_LEAK_xyz"


def _spec(name: str, *, lane: str = "clean_room") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        judge=None,
        lane=lane,
    )


class _CleanRunner:
    def run(self, spec: EvalSpec) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(EvalToolCall(name="Bash", input={"command": SENTINEL}, turn=1),),
            text_blocks=(f"{SENTINEL} reasoning",),
            terminal_reason="end_turn",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.01,
        )


@pytest.fixture
def clean_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _CleanRunner())
    monkeypatch.setattr("teatree.eval.report.find_spec", _spec)


@pytest.mark.usefixtures("clean_runner")
class TestSummaryMdArtifact:
    def test_writes_sanitized_summary(self, tmp_path: Path) -> None:
        out = tmp_path / "summary.md"
        run_pass_at_k_lane(
            [_spec("alpha"), _spec("beta")],
            max_turns=None,
            trials=3,
            require="any",
            output_format="text",
            persist=False,
            summary_md=out,
        )
        body = out.read_text(encoding="utf-8")
        assert "alpha" in body
        assert "beta" in body
        assert "2/3" not in body  # passes counted: clean trials all pass → 3/3
        assert "3/3" in body
        assert SENTINEL not in body

    def test_no_file_when_summary_md_none(self, tmp_path: Path) -> None:
        out = tmp_path / "summary.md"
        run_pass_at_k_lane(
            [_spec("alpha")],
            max_turns=None,
            trials=2,
            require="any",
            output_format="text",
            persist=False,
            summary_md=None,
        )
        assert not out.exists()


class _FailingRunner:
    def run(self, spec: EvalSpec) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=(SENTINEL,),
            terminal_reason="success",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.01,
        )


class TestSummaryWrittenEvenOnRedRun:
    def test_summary_written_before_non_zero_exit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _FailingRunner())
        monkeypatch.setattr("teatree.eval.report.find_spec", _spec)
        spec = EvalSpec(
            name="fails",
            scenario="s",
            agent_path="skills/code/SKILL.md",
            prompt="do",
            matchers=(
                Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="never-emitted"),
            ),
            source_path=Path("/tmp/spec.yaml"),
            judge=None,
        )
        out = tmp_path / "summary.md"
        failed = run_pass_at_k_lane(
            [spec],
            max_turns=None,
            trials=2,
            require="any",
            output_format="text",
            persist=False,
            model_override="claude-sonnet-4-6",
            summary_md=out,
        )
        assert failed is True
        body = out.read_text(encoding="utf-8")
        assert "fails" in body
        assert "fail" in body
        assert SENTINEL not in body
