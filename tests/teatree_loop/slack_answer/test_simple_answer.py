"""Tests for the two-stage SIMPLE answer builder (#1014).

Stage A is zero-token: it renders an answer directly from teatree's own
dashboard/active-ticket state. Stage B is a single ``claude -p --model
haiku`` call, gated by ``T3_SLACK_ANSWER_TOKEN_BUDGET`` and bounded to
return the ``NEEDS_WORK`` sentinel when it cannot answer cheaply. Only
the ``claude`` subprocess is mocked; everything else is real.
"""

from unittest.mock import patch

import pytest

from teatree.core.models import PendingChatInjection
from teatree.loop.slack_answer.simple_answer import NEEDS_WORK_SENTINEL, build_simple_answer

pytestmark = pytest.mark.django_db


def _row(text: str) -> PendingChatInjection:
    row = PendingChatInjection.record(channel="C1", slack_ts="1.0", text=text)
    assert row is not None
    return row


class TestStageADirectState:
    def test_status_question_answered_from_dashboard_without_llm(self) -> None:
        row = _row("what's the status?")
        with (
            patch(
                "teatree.loop.slack_answer.simple_answer.render_dashboard",
                return_value="# Loop dashboard\n\n## [acme]\n| Ref | ... |\n",
            ) as render,
            patch("teatree.loop.slack_answer.simple_answer._run_haiku") as haiku,
        ):
            answer = build_simple_answer(row)

        assert answer is not None
        assert "Loop dashboard" in answer
        render.assert_called_once()
        haiku.assert_not_called()

    def test_pending_question_answered_from_state_without_llm(self) -> None:
        row = _row("what's pending?")
        with (
            patch(
                "teatree.loop.slack_answer.simple_answer.render_dashboard",
                return_value="# Loop dashboard\n\n## [acme]\n| Ref | x |\n",
            ),
            patch("teatree.loop.slack_answer.simple_answer._run_haiku") as haiku,
        ):
            answer = build_simple_answer(row)

        assert answer is not None
        haiku.assert_not_called()


class TestStageBHaikuFallback:
    def test_falls_to_haiku_when_stage_a_returns_none(self) -> None:
        row = _row("which PRs are open?")
        with (
            patch(
                "teatree.loop.slack_answer.simple_answer.render_dashboard",
                return_value="_No tick actions recorded yet._\n",
            ),
            patch(
                "teatree.loop.slack_answer.simple_answer._run_haiku",
                return_value="No open PRs right now.",
            ) as haiku,
            patch(
                "teatree.loop.slack_answer.simple_answer.precheck_budget",
                return_value=_AllowVerdict(),
            ),
        ):
            answer = build_simple_answer(row)

        assert answer == "No open PRs right now."
        haiku.assert_called_once()

    def test_haiku_needs_work_sentinel_propagates(self) -> None:
        row = _row("which PRs are open?")
        with (
            patch(
                "teatree.loop.slack_answer.simple_answer.render_dashboard",
                return_value="_No tick actions recorded yet._\n",
            ),
            patch(
                "teatree.loop.slack_answer.simple_answer._run_haiku",
                return_value=NEEDS_WORK_SENTINEL,
            ),
            patch(
                "teatree.loop.slack_answer.simple_answer.precheck_budget",
                return_value=_AllowVerdict(),
            ),
        ):
            answer = build_simple_answer(row)

        assert answer == NEEDS_WORK_SENTINEL

    def test_token_budget_exhausted_skips_haiku_returns_none(self) -> None:
        row = _row("which PRs are open?")
        with (
            patch(
                "teatree.loop.slack_answer.simple_answer.render_dashboard",
                return_value="_No tick actions recorded yet._\n",
            ),
            patch("teatree.loop.slack_answer.simple_answer._run_haiku") as haiku,
            patch(
                "teatree.loop.slack_answer.simple_answer.precheck_budget",
                return_value=_SkipVerdict(),
            ),
        ):
            answer = build_simple_answer(row)

        assert answer is None
        haiku.assert_not_called()


class _AllowVerdict:
    ok = True
    reason = ""


class _SkipVerdict:
    ok = False
    reason = "token_budget_exhausted"
