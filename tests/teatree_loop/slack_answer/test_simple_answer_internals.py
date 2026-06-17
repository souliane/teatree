"""Coverage for the Stage B / token-budget internals (#1014).

The public ``build_simple_answer`` tests mock ``_run_haiku``; these
exercise the real helper (the in-process Agent SDK mocked at
``claude_agent_sdk.query``) plus the token-budget env parsing and the
Stage-A no-keyword early return.

Stage B runs the haiku model in-process via ``claude_agent_sdk.query`` —
the SAME subscription-authenticated path (``CLAUDE_CODE_OAUTH_TOKEN``) the
eval ``sdk`` backend uses. It never shells ``claude -p`` and never bills an
API key; it spends subscription-covered model time for one stateless turn.
"""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from teatree.loop.slack_answer import simple_answer
from teatree.loop.slack_answer.simple_answer import NEEDS_WORK_SENTINEL, _run_haiku, _stage_a, _token_budget_remaining


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-haiku")


def _fake_query(text: str) -> Any:
    """Return a ``query``-shaped async generator yielding one assistant message."""

    async def _gen(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:  # noqa: RUF029 — async generator matching the SDK `query` signature; the `yield` makes it async.
        yield _assistant(text)

    return _gen


def _raising_query(exc: BaseException) -> Any:
    async def _gen(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:  # noqa: RUF029 — async generator matching the SDK `query` signature; raises mid-stream.
        raise exc
        yield  # pragma: no cover — unreachable, marks this an async generator

    return _gen


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
    def test_missing_sdk_returns_sentinel(self) -> None:
        # No claude CLI child available (the SDK spawns it) → fall through to
        # delegation, exactly as the old missing-binary path did.
        with patch("teatree.loop.slack_answer.simple_answer.shutil.which", return_value=None):
            assert _run_haiku("q", "digest") == NEEDS_WORK_SENTINEL

    def test_query_error_returns_sentinel(self) -> None:
        with (
            patch("teatree.loop.slack_answer.simple_answer.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.loop.slack_answer.simple_answer.query", _raising_query(RuntimeError("boom"))),
        ):
            assert _run_haiku("q", "digest") == NEEDS_WORK_SENTINEL

    def test_timeout_returns_sentinel(self) -> None:
        with (
            patch("teatree.loop.slack_answer.simple_answer.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.loop.slack_answer.simple_answer.query", _raising_query(TimeoutError())),
        ):
            assert _run_haiku("q", "digest") == NEEDS_WORK_SENTINEL

    def test_empty_text_returns_sentinel(self) -> None:
        with (
            patch("teatree.loop.slack_answer.simple_answer.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.loop.slack_answer.simple_answer.query", _fake_query("   ")),
        ):
            assert _run_haiku("q", "digest") == NEEDS_WORK_SENTINEL

    def test_successful_answer_text_returned(self) -> None:
        with (
            patch("teatree.loop.slack_answer.simple_answer.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.loop.slack_answer.simple_answer.query", _fake_query("Two PRs open.\n")),
        ):
            assert _run_haiku("which PRs?", "digest") == "Two PRs open."

    def test_options_select_haiku_single_turn_no_tools(self) -> None:
        captured: dict[str, Any] = {}

        async def _capture(*_args: Any, **kwargs: Any) -> AsyncIterator[Any]:  # noqa: RUF029 — async generator matching the SDK `query` signature; the `yield` makes it async.
            captured["options"] = kwargs.get("options")
            yield _assistant("ok")

        with (
            patch("teatree.loop.slack_answer.simple_answer.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.loop.slack_answer.simple_answer.query", _capture),
        ):
            assert _run_haiku("which PRs?", "digest") == "ok"

        options = captured["options"]
        # Haiku model, a single stateless turn, no tools, and the clean-room
        # isolation the eval runner uses (no personal settings bias the answer).
        assert "haiku" in options.model
        assert options.max_turns == 1
        assert options.tools == []
        assert options.setting_sources == []
        assert options.system_prompt == simple_answer._HAIKU_SYSTEM_PROMPT
