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
phase-independent, so it covers every such phase. Only a reason that names an owner
decision (``user_input_kind``) is an owner question (#5096).
"""

import pytest
from django.test import TestCase

from teatree.core.models import DeferredQuestion, Session, Task, TaskAttempt, Ticket
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.task_handoff import (
    RESUME_ANSWER_PREFIX,
    RESUME_CONTINUATION_CLAUSE,
    dispatch_reason,
    record_deferred_question,
    schedule_resume,
)
from tests._owner_channel import OWNER_CARD, owner_stop
from tests.factories import planned_ticket


class TestRecordDeferredQuestionAudience(TestCase):
    def _headless_task_with_reason(
        self, reason: str, *, phase: str = "scanning_news", kind: str = "", checked: tuple[str, ...] = ()
    ) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        result = owner_stop(reason, kind, *checked) if kind else {"needs_user_input": True, "user_input_reason": reason}
        TaskAttempt.objects.create(task=task, result=result)
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

    def test_a_question_naming_no_owner_decision_is_internal(self) -> None:
        task = self._headless_task_with_reason("Should I merge PR #7 now, or wait for the release branch to cut first?")
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_a_named_owner_decision_reaches_the_owner(self) -> None:
        task = self._headless_task_with_reason(
            "Should I block the review on the missing migration, or accept it and file a follow-up?",
            phase="reviewing",
            kind="product_scope",
            checked=("the ticket names neither option",),
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_declared_kind_with_checked_records_owner_row(self) -> None:
        task = self._headless_task_with_reason(
            "The deploy token expired; mint a new one?",
            kind="credentials",
            checked=("The vault holds no deploy token.",),
        )
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION
        assert row.evidence["decision"] == "credentials"
        assert row.evidence["checked"] == ["The vault holds no deploy token."]
        assert row.evidence["blocker"] == OWNER_CARD.blocker

    def test_an_unknown_kind_names_no_owner_decision(self) -> None:
        task = self._headless_task_with_reason("The gate refused the push.", phase="reviewing", kind="gate_refusal")
        row = record_deferred_question(task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL


class TestAHeadlessStopReachesTheOwnerOnlyForAnOwnerDecision(TestCase):
    """A kind-less stop is recorded internal and resumes nothing; only an owner-channel answer resumes (#5096)."""

    def _stopped(self, ticket: Ticket, reason: str, *, kind: str = "") -> Task:
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing")
        result = owner_stop(reason, kind) if kind else {"needs_user_input": True, "user_input_reason": reason}
        TaskAttempt.objects.create(task=task, result=result)
        task.complete()
        return task

    def test_kindless_stop_records_internal_and_mints_no_task(self) -> None:
        task = self._stopped(planned_ticket(), "The merge gate refused the push; how should I proceed?")

        assert not Task.objects.exclude(pk=task.pk).exists()
        row = DeferredQuestion.objects.get(parked_task=task)
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.is_pending
        assert row.resolved_via == DeferredQuestion.ResolvedVia.UNRESOLVED

    def test_a_tool_lack_self_report_is_never_resumed(self) -> None:
        task = self._stopped(planned_ticket(), "This session lacks any shell tool (no Bash), so I cannot inspect it.")

        assert not task.child_tasks.exists()
        assert DeferredQuestion.objects.get().is_pending

    def test_a_credentials_stop_asks_the_owner_and_queues_nothing(self) -> None:
        task = self._stopped(planned_ticket(), "The deploy token expired; mint a new one?", kind="credentials")

        assert not task.child_tasks.exists()
        row = DeferredQuestion.objects.get()
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION
        assert row.is_pending

    def test_sticky_hit_resumes_only_on_owner_channel_answer(self) -> None:
        reason = "The deploy token expired; mint a new one?"
        via = DeferredQuestion.ResolvedVia
        for channel in (via.SLACK, via.LOCAL, via.AGENT, via.STALE):
            with self.subTest(channel=channel):
                ticket = planned_ticket()
                first = self._stopped(ticket, reason, kind="credentials")
                row = DeferredQuestion.objects.get(parked_task=first)
                if channel == via.STALE:
                    row.mark_stale("the token rotated on its own")
                else:
                    row.apply_answer("use the vault entry", resolved_via=channel)

                second = self._stopped(ticket, reason, kind="credentials")

                assert DeferredQuestion.objects.filter(parked_task__ticket=ticket).count() == 1
                resumed = [child.execution_reason for child in second.child_tasks.all()]
                assert resumed == ([f"{RESUME_ANSWER_PREFIX} use the vault entry."] if channel == via.SLACK else [])
                assert not first.child_tasks.exists()

    def test_a_cosmetic_rewording_of_a_dismissed_owner_stop_is_not_asked_again(self) -> None:
        for variant in ("the deploy token expired; mint a new one?", "The deploy token  expired;  mint a new one? "):
            with self.subTest(variant=variant):
                ticket = planned_ticket()
                first = self._stopped(ticket, "The deploy token expired; mint a new one?", kind="credentials")
                DeferredQuestion.objects.get(parked_task=first).mark_stale("withdrawn by the age ladder")

                self._stopped(ticket, variant, kind="credentials")

                assert DeferredQuestion.objects.filter(parked_task__ticket=ticket).count() == 1
                assert not DeferredQuestion.unmirrored_pending().exists()

    def test_a_different_reason_after_a_dismissed_owner_stop_is_asked(self) -> None:
        ticket = planned_ticket()
        first = self._stopped(ticket, "The deploy token expired; mint a new one?", kind="credentials")
        DeferredQuestion.objects.get(parked_task=first).mark_stale("withdrawn by the age ladder")

        second = self._stopped(ticket, "Which vault holds the signing key?", kind="credentials")

        assert [row.parked_task_id for row in DeferredQuestion.unmirrored_pending()] == [second.pk]


class TestARowStampedBeforeTheClauseMovedOutOfStorage(TestCase):
    """The queue holds resumes whose stored reason still has the clause inline — both cases apply.

    Composing onto one of those blindly would say it twice; leaving them alone would exempt the
    very rows already in flight from the rule the change exists to enforce.
    """

    _LEGACY = f"{RESUME_ANSWER_PREFIX} postgres-1. {RESUME_CONTINUATION_CLAUSE}"

    def _task(self, continuation: str) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(
            ticket=ticket, session=session, execution_reason=self._LEGACY, session_continuation=continuation
        )

    def test_a_resumed_dispatch_is_told_to_continue_exactly_once(self) -> None:
        reason = dispatch_reason(self._task(Task.SessionContinuation.PARENT))

        assert reason.count(RESUME_CONTINUATION_CLAUSE) == 1
        assert "postgres-1" in reason

    def test_a_fresh_dispatch_has_the_stored_clause_taken_off(self) -> None:
        reason = dispatch_reason(self._task(Task.SessionContinuation.FRESH))

        assert RESUME_CONTINUATION_CLAUSE not in reason
        assert "postgres-1" in reason


class TestResumeNeedsAPlan(TestCase):
    def test_resuming_an_implementing_task_on_an_unplanned_ticket_is_refused(self) -> None:
        ticket = Ticket.objects.create()
        parked = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

        with pytest.raises(NoPlanArtifactError, match="plan_missing"):
            schedule_resume(parked, answer="use postgres-1")

        assert not parked.child_tasks.exists()

    def test_a_non_implementing_parked_task_still_resumes(self) -> None:
        ticket = Ticket.objects.create()
        parked = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="planning")

        assert schedule_resume(parked, answer="scope it to X").phase == "planning"
