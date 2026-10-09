"""Tests for the :class:`DeferredQuestion` model (#58, BLUEPRINT §17.1 invariant 9).

Mirrors the ``OnBehalfApproval`` test layout 1:1: every contract clause
the model promises in its docstring is asserted here (guarded factory,
single-use consume, scope of queryset, audit row).
"""

import os
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.test import TestCase

from teatree import answer_handback
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.approval_dial import auto_answer_by_policy
from teatree.core.models.deferred_question import (
    DeferredQuestion,
    DeferredQuestionAudit,
    DeferredQuestionError,
    is_tool_lack_selfreport,
    question_fingerprint,
)
from teatree.instance_id import instance_id

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestDedupeMarker:
    def test_repeat_marker_collapses_to_one_pending_row(self) -> None:
        first = DeferredQuestion.record("stall on ticket 1", dedupe_marker="repair-stall:1:coding")
        second = DeferredQuestion.record("stall on ticket 1", dedupe_marker="repair-stall:1:coding")
        assert first.pk == second.pk
        assert DeferredQuestion.objects.filter(dedupe_marker="repair-stall:1:coding").count() == 1

    def test_eight_identical_reason_clones_collapse_via_fingerprint(self) -> None:
        marker = f"needs-input:{question_fingerprint('I lack the tools to review this PR')}"
        for _ in range(8):
            DeferredQuestion.record("I lack the tools to review this PR", dedupe_marker=marker)
        assert DeferredQuestion.pending().count() == 1

    def test_fingerprint_ignores_whitespace_and_case(self) -> None:
        assert question_fingerprint("I  lack   TOOLS ") == question_fingerprint("i lack tools")

    def test_distinct_markers_do_not_collapse(self) -> None:
        DeferredQuestion.record("q", dedupe_marker="a")
        DeferredQuestion.record("q", dedupe_marker="b")
        assert DeferredQuestion.pending().count() == 2

    def test_empty_marker_never_dedupes(self) -> None:
        DeferredQuestion.record("q")
        DeferredQuestion.record("q")
        assert DeferredQuestion.pending().count() == 2

    def test_internal_marker_stays_pending_only(self) -> None:
        first = DeferredQuestion.record("stall", dedupe_marker="m")
        DeferredQuestion.consume(first.pk, answer="handled")
        second = DeferredQuestion.record("stall again", dedupe_marker="m")
        assert second.pk != first.pk


class TestDeferredQuestionRecord:
    def test_record_creates_a_pending_row(self) -> None:
        row = DeferredQuestion.record(
            "Should I proceed with the refactor?",
            options_json='[{"label": "yes"}, {"label": "no"}]',
            session_id="sess-1",
            tool_use_id="toolu_1",
        )
        assert row.pk is not None
        assert row.is_pending is True
        assert row.status == DeferredQuestion.STATUS_PENDING
        assert row.answered_at is None
        assert row.dismissed_at is None
        assert row.question == "Should I proceed with the refactor?"
        assert row.session_id == "sess-1"
        assert row.tool_use_id == "toolu_1"

    def test_record_strips_and_requires_question(self) -> None:
        with pytest.raises(DeferredQuestionError, match="question is required"):
            DeferredQuestion.record("   ")

    def test_record_keeps_optional_fields_default_empty(self) -> None:
        row = DeferredQuestion.record("Just the text.")
        assert row.options_json == ""
        assert row.session_id == ""
        assert row.tool_use_id == ""


class TestDeferredQuestionAudience:
    """Only a named owner decision with checked evidence reaches the owner; every other question is internal."""

    def test_record_without_decision_is_internal(self) -> None:
        row = DeferredQuestion.record("Repair-loop stall on ticket 1")
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.evidence == {}

    @pytest.mark.parametrize("decision", list(OwnerDecision))
    def test_a_named_decision_makes_an_owner_row(self, decision: OwnerDecision) -> None:
        row = DeferredQuestion.record("Ship it?", decision=decision, checked=["the issue is silent"])
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_owner_row_stores_kind_and_checked(self) -> None:
        DeferredQuestion.record(
            "Rotate the deploy token?",
            decision=OwnerDecision.CREDENTIALS,
            checked=["  the token expired 10-08 ", "", "no standing answer in memory"],
        )
        row = DeferredQuestion.objects.get()
        assert row.evidence == {
            "decision": "credentials",
            "checked": ["the token expired 10-08", "no standing answer in memory"],
        }

    @pytest.mark.parametrize("checked", [(), ["", "   "]])
    def test_decision_without_checked_is_refused_and_writes_no_row(self, checked: list[str]) -> None:
        with pytest.raises(DeferredQuestionError, match="checked"):
            DeferredQuestion.record("Ship it?", decision=OwnerDecision.PUBLIC_POST, checked=checked)
        assert not DeferredQuestion.objects.exists()

    def test_unknown_decision_is_refused(self) -> None:
        with pytest.raises(DeferredQuestionError, match="whim"):
            DeferredQuestion.record("Ship it?", decision=cast("OwnerDecision", "whim"), checked=["looked"])
        assert not DeferredQuestion.objects.exists()

    def test_architecture_kind_exists(self) -> None:
        assert {kind.value for kind in OwnerDecision} == {
            "credentials",
            "money_or_plan",
            "public_post",
            "irreversible",
            "product_scope",
            "architecture",
        }
        row = DeferredQuestion.record(
            "Split the model?", decision=OwnerDecision.ARCHITECTURE, checked=["BLUEPRINT §4 is silent"]
        )
        assert row.evidence["decision"] == "architecture"

    def test_owner_pending_lists_only_pending_owner_rows(self) -> None:
        owner = DeferredQuestion.record("Rotate it?", decision=OwnerDecision.CREDENTIALS, checked=["expired"])
        answered = DeferredQuestion.record("Pay it?", decision=OwnerDecision.MONEY_OR_PLAN, checked=["invoice"])
        DeferredQuestion.consume(answered.pk, answer="yes")
        DeferredQuestion.record("internal stall")
        assert [r.pk for r in DeferredQuestion.owner_pending()] == [owner.pk]

    def test_unmirrored_pending_excludes_internal_rows(self) -> None:
        owner = DeferredQuestion.record("Owner decision?", decision=OwnerDecision.CREDENTIALS, checked=["expired"])
        DeferredQuestion.record("internal stall")
        unmirrored = list(DeferredQuestion.unmirrored_pending())
        assert [r.pk for r in unmirrored] == [owner.pk]


class TestAnOwnerQuestionIsNeverReAsked:
    """An owner marker is sticky across answered and dismissed rows."""

    @pytest.mark.parametrize("resolution", [{"answer": "keep them"}, {"dismissed_reason": "not now"}])
    def test_owner_marker_sticky_answered_and_dismissed(self, resolution: dict[str, str]) -> None:
        first = DeferredQuestion.record(
            "Reclaim the leftovers?", dedupe_marker="m", decision=OwnerDecision.IRREVERSIBLE, checked=["3 dirs"]
        )
        DeferredQuestion.consume(first.pk, **resolution)
        again = DeferredQuestion.record(
            "Reclaim the leftovers?", dedupe_marker="m", decision=OwnerDecision.IRREVERSIBLE, checked=["3 dirs"]
        )
        assert again.pk == first.pk
        assert DeferredQuestion.objects.filter(dedupe_marker="m").count() == 1

    def test_a_resolved_internal_row_does_not_mute_an_owner_record(self) -> None:
        internal = DeferredQuestion.record("stall", dedupe_marker="m")
        DeferredQuestion.consume(internal.pk, answer="handled")
        owner = DeferredQuestion.record(
            "Ship it?", dedupe_marker="m", decision=OwnerDecision.PUBLIC_POST, checked=["review is green"]
        )
        assert owner.pk != internal.pk
        assert owner.audience == DeferredQuestion.Audience.OWNER_QUESTION


class TestToolLackSelfReport:
    """An agent's own "I lack the tools to proceed" report is a dispatch fault (INTERNAL)."""

    @pytest.mark.parametrize(
        "text",
        [
            # The verbatim leak: a scanning-news park that reached the owner's DM.
            (
                "This session lacks any shell/write tool (no Bash, no Write/Edit, no gh) needed to run "
                "`manage.py shell -c record_candidate`, dedupe-check via `gh issue list`, or post the Slack DM."
            ),
            "I have no shell to run the management command.",
            "This agent runs shell-denied, so it cannot file the issue.",
            "This must be picked up by a session with the standard toolset.",
            "I need a session with the tools to complete this.",
            "Missing the gh CLI, so I can't open the PR.",
            # #201 (codex_reviewing) — leaked AFTER the first fix: the fault is
            # reported by its consequence, not the bare "no shell" token.
            (
                "This session was launched without Bash/Edit/Write/Agent tool access, so I cannot "
                "inspect the PR diff locally, make code changes, or run the required "
                "`t3 tool verify-gates` green-proof, nor post the codex adversarial review comment "
                "via `gh pr comment`. Need either the session relaunched with full tool access, or "
                "explicit guidance on how to proceed read-only."
            ),
            # #202 (reviewing) — leaked AFTER the first fix: the fault is reported as
            # its symptom (no shell, internal task-context tools returned nothing).
            (
                "Ticket 187's body/state and the full Slack thread weren't available in this phase "
                "(no shell, TaskGet/TaskList returned nothing for it), so I can't confirm what "
                "concrete action is being asked about beyond a generic acknowledgment."
            ),
            # Isolated signals each phrasing carries, asserted alone so the class is
            # caught even when only one signal is present.
            "I cannot inspect the PR diff locally.",
            "Need the session relaunched with full tool access.",
            "TaskGet/TaskList returned nothing for it.",
            "This phase has no accessible checkout of the repo.",
            "I cannot run the required verify-gates green-proof.",
        ],
    )
    def test_tool_lack_phrasings_are_classified(self, text: str) -> None:
        assert is_tool_lack_selfreport(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Should I merge PR #7 or wait for the release branch?",
            "The design has two viable approaches — which do you prefer?",
            "I don't have enough context about the rollout plan to continue.",
            "Should I write the migration now or in a follow-up?",
            # Genuine review-phase decision — no tool-lack signal — must stay owner.
            "Two findings conflict; should I block the PR or file a follow-up ticket?",
            # Near-miss owner questions that brush the new signal words without being
            # a lack report: deciding to "check out" a branch, a "which tool" pick, a
            # "cannot decide" judgment gap.
            "Should I check out the release branch or stay on main?",
            "Which tool should I use to profile the endpoint?",
            "I cannot decide between the two migration strategies — which do you prefer?",
        ],
    )
    def test_genuine_owner_questions_are_not_classified(self, text: str) -> None:
        assert is_tool_lack_selfreport(text) is False

    def test_classification_ignores_case_and_whitespace(self) -> None:
        assert is_tool_lack_selfreport("  This  session  LACKS  any  SHELL  tool. ") is True


class TestDeferredQuestionPending:
    def test_pending_returns_only_unresolved_rows_oldest_first(self) -> None:
        first = DeferredQuestion.record("first?")
        second = DeferredQuestion.record("second?")
        third = DeferredQuestion.record("third?")
        DeferredQuestion.consume(second.pk, answer="ok")

        pending = list(DeferredQuestion.pending())
        assert [r.pk for r in pending] == [first.pk, third.pk]


class TestDeferredQuestionConsume:
    def test_consume_with_answer_marks_answered_and_returns_row(self) -> None:
        row = DeferredQuestion.record("ship?")
        consumed = DeferredQuestion.consume(row.pk, answer="yes")
        assert consumed is not None
        assert consumed.answered_at is not None
        assert consumed.answer_text == "yes"
        assert consumed.status == DeferredQuestion.STATUS_ANSWERED
        assert consumed.is_pending is False

    def test_consume_with_dismiss_marks_dismissed(self) -> None:
        row = DeferredQuestion.record("ship?")
        consumed = DeferredQuestion.consume(row.pk, dismissed_reason="no longer relevant")
        assert consumed is not None
        assert consumed.dismissed_at is not None
        assert consumed.dismissed_reason == "no longer relevant"
        assert consumed.status == DeferredQuestion.STATUS_DISMISSED

    def test_consume_is_single_use(self) -> None:
        row = DeferredQuestion.record("ship?")
        assert DeferredQuestion.consume(row.pk, answer="yes") is not None
        assert DeferredQuestion.consume(row.pk, answer="yes again") is None

    def test_consume_returns_none_for_unknown_id(self) -> None:
        assert DeferredQuestion.consume(999_999, answer="x") is None

    def test_consume_requires_exactly_one_resolution_kind(self) -> None:
        row = DeferredQuestion.record("ship?")
        with pytest.raises(DeferredQuestionError, match="exactly one"):
            DeferredQuestion.consume(row.pk, answer="", dismissed_reason="")
        with pytest.raises(DeferredQuestionError, match="exactly one"):
            DeferredQuestion.consume(row.pk, answer="a", dismissed_reason="b")


class TestDeferredQuestionAuditRow:
    def test_audit_records_who_what_when(self) -> None:
        row = DeferredQuestion.record("ship?")
        consumed = DeferredQuestion.consume(row.pk, answer="yes")
        assert consumed is not None
        audit = DeferredQuestionAudit.objects.create(
            question=consumed,
            action="answered",
            answer_text="yes",
            resolver_id="souliane",
        )
        assert audit.resolver_id == "souliane"
        assert audit.action == "answered"
        assert audit.answer_text == "yes"


class TestStableNotifyRef:
    """Outward-notification idempotency keys derive from a stable identity, never the local pk.

    Fleet-safety Stage 1: two teatree instances keep independent SQLite, so a
    key built from a local autoincrement pk shifts between them. The resurface /
    mirror drains key their ``BotPing`` idempotency on ``stable_notify_ref``.
    """

    def test_key_is_stable_across_two_instances_with_different_local_pks(self) -> None:
        # Model the same logical question captured under two instances: identical
        # harness tool_use_id, but the local DBs assign different autoincrement
        # pks. A pk-derived key would differ between them (double-post / false
        # dedup); the stable ref must be identical.
        first = DeferredQuestion.record("Approve the merge?", tool_use_id="toolu_shared")
        second = DeferredQuestion.record("Approve the merge?", tool_use_id="toolu_shared")

        assert first.pk != second.pk
        assert first.stable_notify_ref == second.stable_notify_ref
        assert first.stable_notify_ref == "toolu_shared"

    def test_falls_back_to_instance_qualified_pk_never_bare_pk(self) -> None:
        row = DeferredQuestion.record("No harness id here")
        assert row.tool_use_id == ""
        assert row.stable_notify_ref == f"{instance_id()}:{row.pk}"
        assert row.stable_notify_ref != str(row.pk)


class TestStrRepr:
    def test_question_str(self) -> None:
        row = DeferredQuestion.record("Will it scale?")
        assert "deferred-question" in str(row)
        assert "pending" in str(row)

    def test_audit_str(self) -> None:
        row = DeferredQuestion.record("Will it scale?")
        consumed = DeferredQuestion.consume(row.pk, answer="yes")
        assert consumed is not None
        audit = DeferredQuestionAudit.objects.create(
            question=consumed,
            action="answered",
            answer_text="yes",
            resolver_id="souliane",
        )
        assert "deferred-question-audit" in str(audit)
        assert "souliane" in str(audit)


class TestAnAnswerIsPostedToTheSessionThatAsked(TestCase):
    def setUp(self) -> None:
        self.data_home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data_home)}))

    def _asked(self, session_id: str = "s-ask", **extra: object) -> DeferredQuestion:
        return DeferredQuestion.record("Which DB host?", session_id=session_id, run_id="r", generation=1, **extra)

    def test_a_local_answer_from_the_cli_is_posted(self) -> None:
        question = self._asked()

        with self.captureOnCommitCallbacks(execute=True):
            call_command("questions", "answer", question.pk, "use postgres-1")

        assert answer_handback.collect("s-ask") == [{"id": question.pk, "answer": "use postgres-1"}]
        question.refresh_from_db()
        assert question.applied_at is not None

    def test_a_locked_database_while_posting_never_aborts_the_answers(self) -> None:
        first, second = self._asked(), self._asked()
        locked = OperationalError("database is locked")

        with (
            patch.object(DeferredQuestion, "mark_posted", side_effect=locked),
            self.captureOnCommitCallbacks(execute=True),
        ):
            call_command("questions", "answer", first.pk, "use postgres-1", also=[second.pk])

        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.answer_text, second.answer_text) == ("use postgres-1", "use postgres-1")

    def test_a_failed_posted_stamp_is_logged_as_that_not_as_an_unposted_answer(self) -> None:
        question = self._asked()
        locked = OperationalError("database is locked")

        with (
            patch.object(DeferredQuestion, "mark_posted", side_effect=locked),
            self.assertLogs("teatree.core.models.deferred_question", level="WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            DeferredQuestion.consume(question.pk, answer="use postgres-1")

        assert answer_handback.collect("s-ask") == [{"id": question.pk, "answer": "use postgres-1"}]
        assert any("posted" in line and "not stamped" in line for line in logs.output)

    def test_a_policy_answer_is_posted(self) -> None:
        question = self._asked()

        with self.captureOnCommitCallbacks(execute=True):
            auto_answer_by_policy(question, "approve")

        assert answer_handback.collect("s-ask") == [{"id": question.pk, "answer": "approve"}]

    def test_nothing_is_posted_before_the_answer_commits(self) -> None:
        question = self._asked()

        with self.captureOnCommitCallbacks(execute=False):
            DeferredQuestion.consume(question.pk, answer="use postgres-1")

        assert answer_handback.collect("s-ask") == []

    def test_a_dismissed_question_posts_nothing(self) -> None:
        question = self._asked()

        with self.captureOnCommitCallbacks(execute=True):
            DeferredQuestion.consume(question.pk, dismissed_reason="asked twice")

        assert answer_handback.collect("s-ask") == []

    def test_a_question_parked_on_a_task_is_resumed_not_posted(self) -> None:
        ticket = Ticket.objects.create()
        parked = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        question = self._asked(parked_task=parked)

        with self.captureOnCommitCallbacks(execute=True):
            DeferredQuestion.consume(question.pk, answer="use postgres-1")

        assert answer_handback.collect("s-ask") == []
        question.refresh_from_db()
        assert question.applied_at is None


class TestTheAskingSessionIsAClaudeSession:
    def test_a_teatree_session_number_is_refused_as_the_asking_session(self) -> None:
        with pytest.raises(DeferredQuestionError, match="task_session"):
            DeferredQuestion.record("q", session_id="42")
