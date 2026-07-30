"""``record_deferred_question`` audience classification (headless needs-input park).

A headless agent that STOPS with ``needs_user_input`` because its session was
dispatched without the shell / ``gh`` / toolset its own work needs is reporting a
DISPATCH fault, not asking the owner a question. That self-report must be recorded
``INTERNAL`` (logged / statusline-only, never DM'd) — the exact owner-DM leak this
guards reached the owner as "*Pending question* … This session lacks any
shell/write tool …" from a scanning-news park, the recurring architectural-review
daemon (#186), and — after the first fix shipped — two review-phase parks that
reported the same fault by its consequence/symptom instead ("launched without
Bash/Edit/Write/Agent tool access, so I cannot inspect the PR diff", #201; "no
shell, TaskGet/TaskList returned nothing", #202). The classifier is
phase-independent, so it covers every such phase. An ordinary needs-input reason is
a genuine owner question and keeps the default ``OWNER_QUESTION`` audience.
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

    def test_codex_review_no_tool_access_self_report_is_internal(self) -> None:
        # #201: a codex_reviewing park that leaked to the owner's DM AFTER #3488 —
        # the reason reports its consequence ("cannot inspect the PR diff", "make
        # code changes", "run the required verify-gates") and its remedy ("relaunched
        # with full tool access") rather than the bare "no shell" #3488 keyed on.
        task = self._headless_task_with_reason(
            "This session was launched without Bash/Edit/Write/Agent tool access, so I cannot "
            "inspect the PR diff locally, make code changes, or run the required "
            "`t3 tool verify-gates` green-proof, nor post the codex adversarial review comment via "
            "`gh pr comment`. Need either the session relaunched with full tool access, or explicit "
            "guidance on how to proceed read-only.",
            phase="codex_reviewing",
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_review_no_task_context_self_report_is_internal(self) -> None:
        # #202: a reviewing park that leaked AFTER #3488 — the missing capability is
        # reported as its symptom (no shell, TaskGet/TaskList returned nothing), a
        # dispatch fault the owner must never be asked to compensate for.
        task = self._headless_task_with_reason(
            "Ticket 187's body/state and the full Slack thread weren't available in this phase "
            "(no shell, TaskGet/TaskList returned nothing for it), so I can't confirm what concrete "
            "action is being asked about beyond a generic acknowledgment.",
            phase="reviewing",
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_internal_self_report_is_excluded_from_owner_dm_drain(self) -> None:
        # The audience is not cosmetic: an INTERNAL row must never enter the owner DM
        # drain (``unmirrored_pending`` filters to OWNER_QUESTION), so the leak the
        # #201/#202 reports caused cannot recur even once recorded.
        task = self._headless_task_with_reason(
            "This session was launched without Bash/Edit/Write/Agent tool access, so I cannot "
            "inspect the PR diff or run the required verify-gates green-proof.",
            phase="codex_reviewing",
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.pk not in {r.pk for r in DeferredQuestion.unmirrored_pending()}

    def test_ordinary_question_keeps_owner_audience(self) -> None:
        task = self._headless_task_with_reason("Should I merge PR #7 now, or wait for the release branch to cut first?")
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_review_phase_owner_decision_keeps_owner_audience(self) -> None:
        # A genuine reviewing-phase decision question — no tool-lack / dispatch signal
        # — must stay OWNER_QUESTION and reach the owner. The broadening must not
        # sweep real "how should I proceed on X?" questions into INTERNAL.
        task = self._headless_task_with_reason(
            "Two of the review findings conflict — should I block the PR on the missing migration, "
            "or accept it and file a follow-up ticket? Which do you prefer?",
            phase="reviewing",
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION
