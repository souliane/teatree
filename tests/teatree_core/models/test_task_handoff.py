"""``record_deferred_question`` audience classification (headless needs-input park).

A headless agent that STOPS with ``needs_user_input`` because its session was
dispatched without the shell / ``gh`` / toolset its own work needs is reporting a
DISPATCH fault, not asking the owner a question. That self-report must be recorded
``INTERNAL`` (logged / statusline-only, never DM'd) — the exact owner-DM leak this
guards reached the owner as "*Pending question* … This session lacks any
shell/write tool …" from BOTH a scanning-news park and the recurring
architectural-review daemon (#186). The classifier is phase-independent, so it
covers every such phase. An ordinary needs-input reason is a genuine owner question
and keeps the default ``OWNER_QUESTION`` audience.
"""

from django.test import TestCase

from teatree.core.models import DeferredQuestion, Session, Task, TaskAttempt, Ticket
from teatree.core.models.task_handoff import record_deferred_question


class TestRecordDeferredQuestionAudience(TestCase):
    def _headless_task_with_reason(self, reason: str, *, phase: str = "scanning_news") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        TaskAttempt.objects.create(
            task=task,
            execution_target=Task.ExecutionTarget.HEADLESS,
            result={"needs_user_input": True, "user_input_reason": reason},
        )
        return task

    def test_tool_lack_self_report_is_recorded_internal(self) -> None:
        task = self._headless_task_with_reason(
            "This session lacks any shell/write tool (no Bash, no Write/Edit, no gh) needed to "
            "run `manage.py shell -c record_candidate`, dedupe-check via `gh issue list`, or "
            "post the Slack DM per t3:scanning-news."
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_needs_standard_toolset_hand_off_is_internal(self) -> None:
        task = self._headless_task_with_reason(
            "I cannot proceed — this must be picked up by a session with the standard toolset."
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_architectural_review_tool_lack_self_report_is_internal(self) -> None:
        # The recurring architectural-review daemon leaked this exact self-report to
        # the owner's DM (#186). The classifier is phase-independent, so the
        # scanning-news fix already covers this phase — assert it stays INTERNAL.
        task = self._headless_task_with_reason(
            "This session lacks shell (Bash/PowerShell), file-write (Write/Edit), and teatree MCP "
            "tools, and has no accessible checkout of the teatree repo. The architectural-review "
            "ticket requires inspecting git log/PR state, reading and potentially editing "
            "src/teatree, and running `t3 tool verify-gates` — none of which are possible here.",
            phase="architectural_review",
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_ordinary_question_keeps_owner_audience(self) -> None:
        task = self._headless_task_with_reason("Should I merge PR #7 now, or wait for the release branch to cut first?")
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION
