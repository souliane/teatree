"""Stop-hook gate for unanswered user questions (#1063).

Integration tests against the real DB and the real
``handle_enforce_answered_questions`` handler from
:mod:`hooks.scripts.hook_router`. The handler queries
:meth:`PendingChatInjection.unanswered_questions_since` and emits an
``additionalContext`` BLOCKING REMINDER when any heuristic-classified
question from the last hour is unanswered.

**Anti-vacuous mutation evidence:** the
``test_blocking_reminder_appears_for_unanswered_question`` case
guards the ``answered_at__isnull=True`` filter. Removing that filter
in ``PendingChatInjection.unanswered_questions_since`` makes the
already-answered row leak into the result; the assertion that the
reminder is empty after stamping ``answered_at`` turns RED.
"""

import builtins
import io
import json
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from hooks.scripts.hook_router import _HANDLERS, handle_enforce_answered_questions
from teatree.core.models import PendingChatInjection

pytestmark = pytest.mark.django_db


def _run_hook(payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> tuple[bool | None, str]:
    """Invoke the handler with a captured stdout, returning ``(return_value, stdout)``."""
    buf = io.StringIO()
    monkeypatch.setattr("hooks.scripts.hook_router.sys.stdout", buf)
    rv = handle_enforce_answered_questions(payload)
    return rv, buf.getvalue()


class TestUnansweredQuestionEmitsBlockingReminder:
    def test_blocking_reminder_appears_for_unanswered_question(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anti-vacuous: revert ``answered_at__isnull=True`` filter → RED.

        The row is a question and is unanswered, so the reminder must
        appear. After stamping ``answered_at``, a re-fire of the hook
        emits nothing — that's the half that fails when the filter is
        reverted (the answered row would still appear in the result).
        """
        PendingChatInjection.record(
            channel="D",
            slack_ts="1700000000.0001",
            text="why are some tests skipped?",
        )

        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is True
        payload = json.loads(out)
        # #1335: Stop schema rejects ``hookSpecificOutput.additionalContext``;
        # nag rides in top-level ``systemMessage``.
        body = payload["systemMessage"]
        assert "BLOCKING REMINDER" in body
        assert "why are some tests skipped?" in body
        assert "1700000000.0001" in body

    def test_after_stamping_answered_at_reminder_disappears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The same row + ``agent_answered_question`` ⇒ hook is a no-op.

        This is the OTHER half of the anti-vacuous pair: if the gate
        ignores ``answered_at``, this assertion goes RED (the body
        re-appears even after the stamp).
        """
        PendingChatInjection.record(
            channel="D",
            slack_ts="1700000000.0001",
            text="why are some tests skipped?",
        )
        PendingChatInjection.agent_answered_question("1700000000.0001")

        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_multiple_unanswered_questions_listed_separately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1.0001", text="why is this red?")
        PendingChatInjection.record(channel="D", slack_ts="2.0001", text="what about merging?")

        _, out = _run_hook({"session_id": "s1"}, monkeypatch)

        payload = json.loads(out)
        body = payload["systemMessage"]
        assert "why is this red?" in body
        assert "what about merging?" in body
        # The list should claim 2 questions.
        assert "2 user question" in body

    def test_directive_alone_does_not_trigger_reminder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-question row must not trip the gate.

        Directives are out of scope for this specific gate — the gate
        only ever asserts on heuristic-classified questions.
        """
        PendingChatInjection.record(channel="D", slack_ts="1.0001", text="status update please")

        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_stale_question_outside_window_does_not_trigger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = PendingChatInjection.record(channel="D", slack_ts="1.0001", text="why?")
        assert row is not None
        row.received_at = timezone.now() - timedelta(hours=5)
        row.save(update_fields=["received_at"])

        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_stop_hook_active_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``stop_hook_active`` ⇒ no-op (re-fire guard)."""
        PendingChatInjection.record(channel="D", slack_ts="1.0001", text="why?")

        rv, out = _run_hook({"session_id": "s1", "stop_hook_active": True}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_empty_queue_is_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_emits_no_decision_block_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Soft-block only: never emit ``decision: block``.

        Spec: the user might genuinely be done; the gate nags
        prominently but does NOT hard-block the turn end.
        """
        PendingChatInjection.record(channel="D", slack_ts="1.0001", text="why?")

        _, out = _run_hook({"session_id": "s1"}, monkeypatch)

        payload = json.loads(out)
        # Soft-block: top-level ``systemMessage`` (schema-valid), never
        # ``decision: block`` (hard-block). Stop schema also rejects
        # ``hookSpecificOutput.additionalContext`` (#1335).
        assert "decision" not in payload
        assert "hookSpecificOutput" not in payload
        assert isinstance(payload["systemMessage"], str)
        assert payload["systemMessage"]


class TestRouterWiring:
    def test_handler_registered_for_stop(self) -> None:
        names = [h.__name__ for h in _HANDLERS["Stop"]]
        assert "handle_enforce_answered_questions" in names

    def test_runs_after_structured_question_gate(self) -> None:
        """Structured-question gate must run first — it's the dominant Stop block."""
        names = [h.__name__ for h in _HANDLERS["Stop"]]
        assert names.index("handle_enforce_structured_question") < names.index("handle_enforce_answered_questions")

    def test_runs_before_loop_self_pump(self) -> None:
        """Loop self-pump must run AFTER — an unanswered-question turn preempts it."""
        names = [h.__name__ for h in _HANDLERS["Stop"]]
        assert names.index("handle_enforce_answered_questions") < names.index("handle_loop_self_pump")


class TestCrashProof:
    """The Stop hook must never raise into the session (#810 contract)."""

    def test_handler_swallows_unexpected_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        boom = RuntimeError("boom")

        def _boom(_window: timedelta) -> list[PendingChatInjection]:
            raise boom

        monkeypatch.setattr(
            PendingChatInjection,
            "unanswered_questions_since",
            classmethod(lambda cls, window: _boom(window)),
        )

        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        # No crash; clean None; no stdout write.
        assert rv is None
        assert out == ""

    def test_outer_wrapper_swallows_pre_query_errors(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An error before the inner try (here: bootstrap) hits the outer guard.

        The outer ``try/except`` in ``handle_enforce_answered_questions``
        must catch anything ``_enforce_answered_questions`` raises and
        return ``None`` with only a stderr breadcrumb — never propagate.
        """
        bootstrap_error = RuntimeError("bootstrap exploded")

        def _bootstrap_boom() -> bool:
            raise bootstrap_error

        monkeypatch.setattr("hooks.scripts.hook_router.bootstrap_teatree_django", _bootstrap_boom)

        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is None
        assert out == ""
        assert "enforce-answered-questions skipped" in capsys.readouterr().err

    def test_bootstrap_unavailable_is_quiet_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When Django can't be bootstrapped the gate fails open (quiet)."""
        monkeypatch.setattr("hooks.scripts.hook_router.bootstrap_teatree_django", lambda: False)

        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_model_import_failure_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed model import inside the gate is swallowed (fail-open)."""
        real_import = builtins.__import__
        import_error = ImportError("simulated import failure")

        def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "teatree.core.models.pending_chat_injection":
                raise import_error
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked_import)

        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is None
        assert out == ""
