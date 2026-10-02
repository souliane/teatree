"""Inject an answered-not-yet-applied ``DeferredQuestion`` once (#1174).

The apply leg: once a Slack reply (or a local ``questions answer``) has
resolved a ``DeferredQuestion``, the next ``UserPromptSubmit`` emits the
answer into ``additionalContext`` so the agent picks it up, and stamps
``applied_at`` (single-use CAS) so it surfaces exactly once. This is the
"AskUserQuestion result applied" success state, and it closes the latent
away-mode gap where an answer was stored but never delivered back.
"""

import pytest

import hooks.scripts.hook_router as router
from teatree.core.models.deferred_question import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _stdout(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out.strip()


class TestInjectAnsweredQuestions:
    def test_answered_not_applied_injects_once_and_stamps_applied_at(self, capsys: pytest.CaptureFixture[str]) -> None:
        row = DeferredQuestion.record("Ship it?", session_id="s", run_id="r", generation=1)
        row.apply_answer("Yes", resolved_via="slack")

        router.handle_inject_pending_questions({"session_id": "s"})

        out = _stdout(capsys)
        assert f"#{row.pk}" in out
        assert "Yes" in out
        assert "answered by the user" in out.lower()
        row.refresh_from_db()
        assert row.applied_at is not None

    def test_second_drain_emits_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        row = DeferredQuestion.record("Ship it?", session_id="s", run_id="r", generation=1)
        row.apply_answer("Yes", resolved_via="slack")

        router.handle_inject_pending_questions({"session_id": "s"})
        capsys.readouterr()
        router.handle_inject_pending_questions({"session_id": "s"})

        out = _stdout(capsys)
        assert "answered by the user" not in out.lower()

    def test_away_answered_via_local_cli_also_injected(self, capsys: pytest.CaptureFixture[str]) -> None:
        row = DeferredQuestion.record("Ship it?", session_id="s", run_id="r", generation=1)
        row.apply_answer("Hold", resolved_via="local")

        router.handle_inject_pending_questions({"session_id": "s"})

        out = _stdout(capsys)
        assert "Hold" in out
        row.refresh_from_db()
        assert row.applied_at is not None

    def test_unanswered_pending_question_is_not_injected_as_applied(self, capsys: pytest.CaptureFixture[str]) -> None:
        DeferredQuestion.record("Pending?", session_id="s", run_id="r", generation=1)

        router.handle_inject_pending_questions({"session_id": "s"})

        out = _stdout(capsys)
        assert "answered by the user" not in out.lower()

    def test_concurrent_drain_applies_at_most_once(self) -> None:
        row = DeferredQuestion.record("Ship it?", session_id="s", run_id="r", generation=1)
        row.apply_answer("Yes", resolved_via="slack")

        applied = DeferredQuestion.mark_applied(row.pk)
        second = DeferredQuestion.mark_applied(row.pk)

        assert applied is True
        assert second is False
