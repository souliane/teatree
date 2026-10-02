"""Coverage for the Stage B / token-budget internals (#1014).

The public ``build_simple_answer`` tests mock ``_run_cheap_turn``; these
exercise the real helper (the shared one-shot seam
``teatree.agents.one_shot.run_one_shot`` mocked at the module boundary) plus
the token-budget env parsing and the Stage-A no-keyword early return.

Stage B runs ONE clean-room, cheap-tier turn through the harness seam — so it
follows a swapped tier-model DB row and works off-Claude. The seam returns
``None`` on ANY failure (missing binary, credential problem, timeout, backend
error), which collapses to the NEEDS_WORK sentinel here. The seam's own
clean-room-options + failure contract is proved in
``tests/teatree_agents/test_one_shot.py``.
"""

from unittest.mock import patch

import pytest

from teatree.loop.slack_answer.simple_answer import (
    NEEDS_WORK_SENTINEL,
    _run_cheap_turn,
    _stage_a,
    _token_budget_remaining,
)


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


class TestRunCheapTurn:
    def test_seam_failure_returns_sentinel(self) -> None:
        # The seam returns None on ANY failure (missing binary, timeout, backend
        # error) → the caller falls through to delegation, as the old
        # missing-binary path did.
        with patch("teatree.loop.slack_answer.simple_answer.run_one_shot", return_value=None):
            assert _run_cheap_turn("q", "digest") == NEEDS_WORK_SENTINEL

    def test_successful_answer_text_returned(self) -> None:
        with patch("teatree.loop.slack_answer.simple_answer.run_one_shot", return_value="Two PRs open.") as one_shot:
            assert _run_cheap_turn("which PRs?", "digest") == "Two PRs open."
        # The turn rides the CHEAP tier through the seam (never a hardcoded id).
        (_prompt, spec), _kwargs = one_shot.call_args
        assert spec.tier == "cheap"
        assert spec.max_turns == 1

    def test_prompt_carries_question_and_digest(self) -> None:
        with patch("teatree.loop.slack_answer.simple_answer.run_one_shot", return_value="ok") as one_shot:
            assert _run_cheap_turn("which PRs?", "3 PRs, 1 blocked") == "ok"
        prompt = one_shot.call_args.args[0]
        assert "which PRs?" in prompt
        assert "3 PRs, 1 blocked" in prompt
