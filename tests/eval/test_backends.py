"""Pluggable eval execution backends (SDK vs subscription-transcript)."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.eval.auth import OAUTH_TOKEN_ENV
from teatree.eval.backends import (
    SDK_BACKEND,
    SUBSCRIPTION_BACKEND,
    SubscriptionTranscriptRunner,
    UnknownBackendError,
    make_runner,
)
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.sdk_runner import MAX_BUDGET_USD, SdkInProcessRunner

FIXTURES = Path(__file__).parent / "fixtures"


def _spec(
    tmp_path: Path,
    *,
    name: str = "worktree_first",
    match_value: str = "git",
) -> EvalSpec:
    agent = tmp_path / "agent.md"
    agent.write_text("# fake skill\n", encoding="utf-8")
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path=str(agent),
        prompt="Fix README typo.",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value=match_value),),
        source_path=tmp_path / "spec.yaml",
    )


class TestMakeRunner:
    def test_sdk_backend_builds_in_process_sdk_runner(self) -> None:
        assert isinstance(make_runner(SDK_BACKEND), SdkInProcessRunner)

    def test_sdk_backend_default_budget_is_the_cheap_cap(self) -> None:
        runner = make_runner(SDK_BACKEND)
        assert isinstance(runner, SdkInProcessRunner)
        assert runner._max_budget_usd == pytest.approx(float(MAX_BUDGET_USD))

    def test_sdk_backend_threads_the_budget_override(self) -> None:
        runner = make_runner(SDK_BACKEND, max_budget_usd=2.0)
        assert isinstance(runner, SdkInProcessRunner)
        assert runner._max_budget_usd == pytest.approx(2.0)

    def test_subscription_backend_builds_transcript_runner(self, tmp_path: Path) -> None:
        runner = make_runner(SUBSCRIPTION_BACKEND, transcript_dir=tmp_path)
        assert isinstance(runner, SubscriptionTranscriptRunner)

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(UnknownBackendError):
            make_runner("magic")

    def test_sdk_backend_resolves_oauth_token_from_pass_when_env_absent(self) -> None:
        # The host sdk runner authenticates from CLAUDE_CODE_OAUTH_TOKEN via the
        # isolated env copy; make_runner must export it from pass so the operator
        # need not. (Local default: just works.)
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.eval.auth.read_pass", return_value="pass-token"),
        ):
            os.environ.pop(OAUTH_TOKEN_ENV, None)
            make_runner(SDK_BACKEND)
            assert os.environ.get(OAUTH_TOKEN_ENV) == "pass-token"

    def test_subscription_backend_does_not_touch_pass(self) -> None:
        # The free subscription lane never authenticates claude -p — it must not
        # read the secret store at all.
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.eval.auth.read_pass") as read_pass,
        ):
            os.environ.pop(OAUTH_TOKEN_ENV, None)
            make_runner(SUBSCRIPTION_BACKEND)
            read_pass.assert_not_called()


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


class TestSubscriptionRunnerGradesSessionSchemaSubagent:
    """A genuinely-produced in-session sub-agent JSONL grades on its matchers.

    The real subscription transcript Claude Code writes under
    ``~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl`` carries the
    session envelope (``isSidechain`` / ``agentId``), has NO top-level ``result``
    event, and ends on the final assistant message's ``stop_reason`` (often
    ``null`` on disk). The pre-fix backend parsed it with the ``claude -p``
    stream-json extractors, whose ``extract_terminal_reason`` returns
    ``("aborted", True)`` when no ``result`` event is present — so every honest
    transcript spurious-failed as an errored run instead of grading on matchers.
    """

    def test_session_schema_subagent_grades_on_matchers_not_aborted(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, match_value="git worktree add")
        transcript = (FIXTURES / "worktree_first_subagent.session.jsonl").read_text(encoding="utf-8")
        (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")

        run = SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec)

        # The defect (RED on current main): no result event -> ("aborted", True).
        assert run.terminal_reason != "aborted"
        assert run.is_error is False
        assert any("git worktree add" in call.input.get("command", "") for call in run.tool_calls)

    def test_session_schema_subagent_grades_to_real_pass(self, tmp_path: Path) -> None:
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path, match_value="git worktree add")
        transcript = (FIXTURES / "worktree_first_subagent.session.jsonl").read_text(encoding="utf-8")
        (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")

        result = evaluate(spec, SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec))

        assert not result.skipped
        assert result.passed

    def test_session_schema_subagent_grades_to_real_fail_when_behavior_absent(self, tmp_path: Path) -> None:
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path, match_value="this command never appears in the transcript")
        transcript = (FIXTURES / "worktree_first_subagent.session.jsonl").read_text(encoding="utf-8")
        (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")

        result = evaluate(spec, SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec))

        # Not a spurious error-fail: it's a real matcher fail, not skipped, not is_error.
        assert not result.skipped
        assert not result.run.is_error
        assert not result.passed
