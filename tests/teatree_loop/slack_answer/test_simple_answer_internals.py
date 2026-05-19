"""Coverage for the Stage B / token-budget internals (#1014).

The public ``build_simple_answer`` tests mock ``_run_haiku``; these
exercise the real helper (subprocess mocked at ``run_checked``) plus the
token-budget env parsing and the Stage-A no-keyword early return.
"""

from unittest.mock import patch

import pytest

from teatree.loop.slack_answer import simple_answer
from teatree.loop.slack_answer.simple_answer import NEEDS_WORK_SENTINEL, _run_haiku, _stage_a, _token_budget_remaining
from teatree.utils.run import CommandFailedError


class TestTokenBudgetEnv:
    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_SLACK_ANSWER_TOKEN_BUDGET", raising=False)
        assert _token_budget_remaining() is None

    def test_integer_value_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_TOKEN_BUDGET", "500")
        assert _token_budget_remaining() == 500

    def test_garbage_value_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_TOKEN_BUDGET", "notanint")
        assert _token_budget_remaining() is None


class TestStageANoKeyword:
    def test_question_without_state_keyword_returns_none(self) -> None:
        # No status/pr/pending/digest token → Stage A bails before render.
        assert _stage_a("how are you feeling") is None


class TestRunHaiku:
    def test_missing_binary_returns_sentinel(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _run_haiku("q", "digest") == NEEDS_WORK_SENTINEL

    def test_subprocess_failure_returns_sentinel(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "teatree.loop.slack_answer.simple_answer.run_checked",
                side_effect=CommandFailedError(["claude"], 1, "", "boom"),
            ),
        ):
            assert _run_haiku("q", "digest") == NEEDS_WORK_SENTINEL

    def test_timeout_returns_sentinel(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "teatree.loop.slack_answer.simple_answer.run_checked",
                side_effect=TimeoutError(),
            ),
        ):
            assert _run_haiku("q", "digest") == NEEDS_WORK_SENTINEL

    def test_empty_stdout_returns_sentinel(self) -> None:
        result = type("R", (), {"stdout": "   "})()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.loop.slack_answer.simple_answer.run_checked", return_value=result),
        ):
            assert _run_haiku("q", "digest") == NEEDS_WORK_SENTINEL

    def test_successful_answer_text_returned(self) -> None:
        result = type("R", (), {"stdout": "Two PRs open.\n"})()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "teatree.loop.slack_answer.simple_answer.run_checked",
                return_value=result,
            ) as run,
        ):
            assert _run_haiku("which PRs?", "digest") == "Two PRs open."

        cmd = run.call_args.args[0]
        assert "--model" in cmd
        assert "haiku" in cmd
        assert "--append-system-prompt" in cmd
        # Single-shot: no skills/tools/loop-context flags.
        assert "--resume" not in cmd
        assert simple_answer._HAIKU_SYSTEM_PROMPT in cmd
