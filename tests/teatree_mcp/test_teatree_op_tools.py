"""Tests for the teatree-op fill-in MCP tools (#3076 / #35).

``question_list`` (read) mirrors the pending ``DeferredQuestion`` backlog;
``task_create`` (write) rides the ``tasks create`` command seam so the dispatch
gates keep their semantics, and a bad ticket surfaces the command's own message
as a structured error (never a ``SystemExit`` that kills the tool call);
``notify_user`` (write) routes through the audited ``teatree.core.notify`` egress.
"""

from typing import Any
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.core.models import DeferredQuestion, Task
from teatree.core.notify import NotifyOutcome, NotifyReason
from teatree.mcp import build_server
from tests.factories import TicketFactory


def _call(tool: str, args: dict[str, Any]) -> Any:
    result = async_to_sync(build_server().call_tool)(tool, args)
    structured = result[1] if isinstance(result, tuple) else result
    return structured["result"] if isinstance(structured, dict) and set(structured) == {"result"} else structured


class TestQuestionList(TestCase):
    def test_lists_pending_questions_only(self) -> None:
        pending = DeferredQuestion.record("Proceed with the rollout?")
        answered = DeferredQuestion.record("Already handled?")
        answered.answered_at = answered.created_at
        answered.answer_text = "yes"
        answered.save(update_fields=["answered_at", "answer_text"])

        result = _call("question_list", {})

        ids = {row["id"] for row in result}
        assert pending.pk in ids
        assert answered.pk not in ids
        assert next(row for row in result if row["id"] == pending.pk)["question"] == "Proceed with the rollout?"


class TestTaskCreate(TestCase):
    def test_creates_a_phase_task_for_the_ticket(self) -> None:
        ticket = TicketFactory()

        result = _call(
            "task_create",
            {"ticket": ticket.pk, "phase": "coding", "reason": "Implement the widget."},
        )

        assert result["ok"] is True
        task = Task.objects.get(pk=result["task_id"])
        assert task.phase == "coding"
        assert task.ticket_id == ticket.pk

    def test_unknown_ticket_surfaces_structured_error_not_systemexit(self) -> None:
        # `tasks create` raises SystemExit on a missing ticket — a BaseException
        # FastMCP does NOT wrap. Without the _run_command guard the tool call
        # crashes; pytest.raises(Exception) would not catch a bare SystemExit,
        # so this is RED on an unguarded handler.
        with pytest.raises(Exception, match="not found"):
            _call("task_create", {"ticket": 999999, "phase": "coding", "reason": "x"})

    def test_missing_phase_surfaces_structured_error(self) -> None:
        ticket = TicketFactory()
        with pytest.raises(Exception, match="phase is required"):
            _call("task_create", {"ticket": ticket.pk, "reason": "x"})


class TestNotifyUser(TestCase):
    def test_routes_through_the_audited_notify_egress(self) -> None:
        with patch(
            "teatree.mcp.write_tools.notify_user_outcome",
            return_value=NotifyOutcome(sent=True),
        ) as seam:
            result = _call(
                "notify_user",
                {"text": "build is green", "idempotency_key": "mcp-test-1"},
            )

        assert result["ok"] is True
        assert seam.call_args.kwargs["idempotency_key"] == "mcp-test-1"
        assert seam.call_args.args[0] == "build is green"

    def test_a_non_delivery_names_its_reason_instead_of_a_bare_false(self) -> None:
        """A bare ``sent=false`` is unactionable — the caller cannot escalate on it."""
        with patch(
            "teatree.mcp.write_tools.notify_user_outcome",
            return_value=NotifyOutcome(sent=False, reason=NotifyReason.NO_MESSAGING_BACKEND),
        ):
            result = _call(
                "notify_user",
                {"text": "five reviews are done", "idempotency_key": "mcp-test-2"},
            )

        assert result["sent"] is False
        assert result["reason"] == "no_messaging_backend"
        assert result["detail"] == NotifyReason.NO_MESSAGING_BACKEND.detail

    def test_an_unknown_kind_is_refused_with_the_valid_set_named(self) -> None:
        # NotifyKind('action_required') used to escape as a bare enum traceback
        # ("'action_required' is not a valid NotifyKind") — loud but unactionable.
        with pytest.raises(Exception, match="valid kinds") as exc_info:
            _call(
                "notify_user",
                {"text": "act on this", "kind": "action_required", "idempotency_key": "mcp-kind-1"},
            )

        assert "answer | question | info" in str(exc_info.value)
        assert "kind='question'" in str(exc_info.value)
