"""Pluggable eval execution backends (SDK vs subscription-transcript)."""

from pathlib import Path

import pytest

from teatree.eval.backends import (
    SDK_BACKEND,
    SUBSCRIPTION_BACKEND,
    SubscriptionTranscriptRunner,
    UnknownBackendError,
    make_runner,
)
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.runner import ClaudePRunner

FIXTURES = Path(__file__).parent / "fixtures"


def _spec(tmp_path: Path, *, name: str = "worktree_first") -> EvalSpec:
    agent = tmp_path / "agent.md"
    agent.write_text("# fake skill\n", encoding="utf-8")
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path=str(agent),
        prompt="Fix README typo.",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git"),),
        source_path=tmp_path / "spec.yaml",
    )


class TestMakeRunner:
    def test_sdk_backend_builds_claude_p_runner(self) -> None:
        assert isinstance(make_runner(SDK_BACKEND), ClaudePRunner)

    def test_subscription_backend_builds_transcript_runner(self, tmp_path: Path) -> None:
        runner = make_runner(SUBSCRIPTION_BACKEND, transcript_dir=tmp_path)
        assert isinstance(runner, SubscriptionTranscriptRunner)

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(UnknownBackendError):
            make_runner("magic")


class TestSubscriptionTranscriptRunner:
    def test_grades_a_subscription_produced_transcript(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        transcript = (FIXTURES / "worktree_first_pass.stream.jsonl").read_text(encoding="utf-8")
        (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")

        run = SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec)

        # Same extractors as the SDK path → tool calls are captured for grading.
        assert any(call.name == "Bash" for call in run.tool_calls)
        assert not run.terminal_reason.startswith("skipped")

    def test_missing_transcript_yields_skip(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        run = SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec)
        assert run.terminal_reason.startswith("skipped")
        assert run.tool_calls == ()
        assert run.is_error is False

    def test_transcript_path_is_named_after_scenario(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, name="my_scenario")
        path = SubscriptionTranscriptRunner(transcript_dir=tmp_path).transcript_path(spec)
        assert path == tmp_path / "my_scenario.jsonl"
