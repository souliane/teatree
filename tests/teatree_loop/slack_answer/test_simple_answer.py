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

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _row(text: str) -> PendingChatInjection:
    row = PendingChatInjection.record(channel="C1", slack_ts="1.0", text=text)
    assert row is not None
    return row


class TestStageADirectState:
    def test_status_question_answered_from_statusline_without_llm(self) -> None:
        row = _row("what's the status?")
        with (
            patch(
                "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
                return_value="overlay=teatree\nPR #545: feat(loop)\n",
            ) as get_statusline,
            patch("teatree.loop.slack_answer.simple_answer._run_haiku") as haiku,
        ):
            answer = build_simple_answer(row)

        assert answer is not None
        assert "overlay=teatree" in answer
        assert "PR #545: feat(loop)" in answer
        get_statusline.assert_called_once()
        haiku.assert_not_called()

    def test_pending_question_answered_from_state_without_llm(self) -> None:
        row = _row("what's pending?")
        with (
            patch(
                "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
                return_value="overlay=teatree\nticket=#1121\n",
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
                "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
                return_value="",
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
                "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
                return_value="",
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
                "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
                return_value="",
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


class TestStageAReturnsStatuslineNotDashboard:
    """Stage A must return the statusline content, NOT the dashboard table (#1121)."""

    def _write_statusline(self, tmp_path, monkeypatch, body: str) -> None:
        from teatree.loop import statusline  # noqa: PLC0415

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        path = statusline.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_stage_a_returns_statusline_not_dashboard(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Real statusline file content the user expects to see verbatim.
        self._write_statusline(tmp_path, monkeypatch, "overlay=teatree\nPR #999: feat(loop) hello\n")
        row = _row("what's the status?")
        with patch("teatree.loop.slack_answer.simple_answer._run_haiku") as haiku:
            answer = build_simple_answer(row)

        assert answer is not None
        assert "overlay=teatree" in answer
        assert "PR #999: feat(loop) hello" in answer
        # Dashboard table markers (markdown row delimiter "| Ref |" etc.) must
        # NOT appear — the bug was Stage A returning render_dashboard() output.
        assert "| Ref |" not in answer
        haiku.assert_not_called()

    def test_stage_a_does_not_return_dashboard_table_for_dashboard_keyword(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: a message containing the "dashboard" token from
        # _DASHBOARD_TOKENS used to make Stage A reply with the dashboard
        # table. The fix must return statusline content instead, regardless
        # of which dashboard keyword matched.
        self._write_statusline(tmp_path, monkeypatch, "overlay=teatree\nticket=#1121\n")
        row = _row("hey what is this loop dashboard")
        with patch("teatree.loop.slack_answer.simple_answer._run_haiku") as haiku:
            answer = build_simple_answer(row)

        assert answer is not None
        assert "overlay=teatree" in answer
        assert "ticket=#1121" in answer
        # No dashboard table markers leaked through.
        assert "| Ref |" not in answer
        assert "# Loop dashboard" not in answer
        haiku.assert_not_called()

    def test_stage_a_returns_none_when_statusline_empty(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Empty statusline (and no file) means Stage A must yield None so
        # the caller falls through to Stage B / NEEDS_WORK delegation.
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        row = _row("what's the status?")
        with (
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

        # Stage A returned None → fell through to Stage B haiku which
        # returned NEEDS_WORK. The assertion is that we did NOT short-circuit
        # with a stale statusline answer.
        assert answer == NEEDS_WORK_SENTINEL


class _AllowVerdict:
    ok = True
    reason = ""


class _SkipVerdict:
    ok = False
    reason = "token_budget_exhausted"
