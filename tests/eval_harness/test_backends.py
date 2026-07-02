"""Pluggable eval execution backends (SDK fresh-run vs recorded transcript)."""

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.eval.api_runner import MAX_BUDGET_USD, ApiInProcessRunner
from teatree.eval.backends import API_BACKEND, TRANSCRIPT_BACKEND, TranscriptRunner, UnknownBackendError, make_runner
from teatree.eval.models import EvalSpec, Matcher
from teatree.llm.credentials import AnthropicSubscriptionCredential

# The DEFAULT eval lane rides the subscription OAuth token (reverses #2707).
OAUTH_ENV = AnthropicSubscriptionCredential().spec.env_var

FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


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
    @pytest.fixture(autouse=True)
    def _bypass_credential_routing(self) -> Iterator[None]:
        # ``make_runner`` resolves its credential through ``resolve_eval_credential``,
        # which reads the ``eval_credential`` setting (DB) and the routing store.
        # Bypass it to the DEFAULT (subscription OAuth) credential so this lane is
        # exercised DB-free — the credential-KIND selection has its own tests.
        with patch("teatree.credential_config.resolve_eval_credential", lambda **_: AnthropicSubscriptionCredential()):
            yield

    def test_api_backend_builds_in_process_api_runner(self) -> None:
        with patch.dict(os.environ, {OAUTH_ENV: "oauth-test"}, clear=False):
            assert isinstance(make_runner(API_BACKEND), ApiInProcessRunner)

    def test_api_backend_default_budget_is_the_cheap_cap(self) -> None:
        with patch.dict(os.environ, {OAUTH_ENV: "oauth-test"}, clear=False):
            runner = make_runner(API_BACKEND)
        assert isinstance(runner, ApiInProcessRunner)
        assert runner._max_budget_usd == pytest.approx(float(MAX_BUDGET_USD))

    def test_api_backend_threads_the_budget_override(self) -> None:
        with patch.dict(os.environ, {OAUTH_ENV: "oauth-test"}, clear=False):
            runner = make_runner(API_BACKEND, max_budget_usd=2.0)
        assert isinstance(runner, ApiInProcessRunner)
        assert runner._max_budget_usd == pytest.approx(2.0)

    def test_transcript_backend_builds_transcript_runner(self, tmp_path: Path) -> None:
        runner = make_runner(TRANSCRIPT_BACKEND, transcript_dir=tmp_path)
        assert isinstance(runner, TranscriptRunner)

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(UnknownBackendError):
            make_runner("magic")

    def test_api_backend_resolves_the_credential_from_pass_when_env_absent(self) -> None:
        # The host api runner authenticates from the SELECTED eval credential
        # (default OAuth) via the isolated env copy; make_runner must export it from
        # pass so the operator need not. (Local default: just works.)
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass", return_value="oauth-pass-token"),
        ):
            os.environ.pop(OAUTH_ENV, None)
            make_runner(API_BACKEND)
            assert os.environ.get(OAUTH_ENV) == "oauth-pass-token"

    def test_transcript_backend_does_not_touch_pass(self) -> None:
        # The transcript lane runs no model — it must not read the secret store.
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("teatree.llm.credentials.read_pass") as read_pass,
        ):
            os.environ.pop(OAUTH_ENV, None)
            make_runner(TRANSCRIPT_BACKEND)
            read_pass.assert_not_called()


class TestTranscriptRunner:
    def test_grades_a_subscription_produced_transcript(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        transcript = (FIXTURES / "worktree_first_pass.stream.jsonl").read_text(encoding="utf-8")
        (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")

        run = TranscriptRunner(transcript_dir=tmp_path).run(spec)

        # Same extractors as the SDK path → tool calls are captured for grading.
        assert any(call.name == "Bash" for call in run.tool_calls)
        assert not run.terminal_reason.startswith("skipped")

    def test_missing_transcript_yields_skip(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
        assert run.terminal_reason.startswith("skipped")
        assert run.tool_calls == ()
        assert run.is_error is False

    def test_transcript_path_is_named_after_scenario(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, name="my_scenario")
        path = TranscriptRunner(transcript_dir=tmp_path).transcript_path(spec)
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

        run = TranscriptRunner(transcript_dir=tmp_path).run(spec)

        # The defect (RED on current main): no result event -> ("aborted", True).
        assert run.terminal_reason != "aborted"
        assert run.is_error is False
        assert any("git worktree add" in call.input.get("command", "") for call in run.tool_calls)

    def test_session_schema_subagent_grades_to_real_pass(self, tmp_path: Path) -> None:
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path, match_value="git worktree add")
        transcript = (FIXTURES / "worktree_first_subagent.session.jsonl").read_text(encoding="utf-8")
        (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")

        result = evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec))

        assert not result.skipped
        assert result.passed

    def test_session_schema_subagent_grades_to_real_fail_when_behavior_absent(self, tmp_path: Path) -> None:
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path, match_value="this command never appears in the transcript")
        transcript = (FIXTURES / "worktree_first_subagent.session.jsonl").read_text(encoding="utf-8")
        (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")

        result = evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec))

        # Not a spurious error-fail: it's a real matcher fail, not skipped, not is_error.
        assert not result.skipped
        assert not result.run.is_error
        assert not result.passed
