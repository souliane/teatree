"""An owner's Slack answer carries out its action once, after rechecking the live evidence outside the write lock."""

import io
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase
from django.utils import timezone

from teatree.backends.slack import receiver
from teatree.core.answering.owner_actions import carry_out_owner_actions
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.merge.void_request import request_void
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import (
    DeferredQuestion,
    DeferredQuestionAudit,
    DmContext,
    PendingChatInjection,
    TaskAttempt,
    Worktree,
)
from teatree.core.models.approval_dial import auto_answer_by_policy
from teatree.core.owner_question_message import bump_text, render_text
from teatree.core.worktree.checkout_attest import CheckoutAttestation, ask_owner_to_attest, read_attestation
from teatree.core.worktree.checkout_fence import claim_task_checkout_or_park, park_task_checkout, verify_task_checkout
from teatree.core.worktree.checkout_state import checkout_state
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.question_binding import BoundAnswer, answer_from_click, apply_bound_answer, bind_reply, resolve_answer
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch
from tests.teatree_loop.test_question_binding import _statement
from tests.teatree_mcp.test_write_tools import _call
from tests.void_merge_fixtures import PR_URL, landed_ticket, stub_forge

_OWNER = "U0DEMOOWNER"
_CLEAR = ProcessTable(frozenset(), "/proc")
_OPTION_INDEX = {"Accept": 1, "Decline": 2, "Proceed": 1, "Void": 1}


def _tap(row: DeferredQuestion, label: str, *, user: str = _OWNER) -> dict[str, object]:
    index = _OPTION_INDEX[label]
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": row.slack_channel},
        "message": {"ts": row.slack_ts},
        "actions": [{"action_id": f"owner-question-option-{index}", "value": str(index), "text": {"text": label}}],
    }


def _owner_backend() -> MagicMock:
    backend = MagicMock(user_id=_OWNER)
    backend.update_message.return_value = {"ok": True}
    return backend


class TestCheckoutAttestation(TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout"
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=0)
        self.task = TaskFactory(ticket=TicketFactory(), phase="coding")
        self.worktree = WorktreeFactory(ticket=self.task.ticket, extra={"worktree_path": str(self.checkout)})

    def _park_on_a_foreign_edit(self) -> DeferredQuestion:
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=_CLEAR):
            verify_task_checkout(self.task, str(self.checkout))
            (self.checkout / "new.txt").write_text("foreign edit")
            assert claim_task_checkout_or_park(self.task) is not None
        row = self.task.deferred_questions.get()
        assert row.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000100")
        return row

    def _answer(self, row: DeferredQuestion, label: str) -> None:
        assert answer_from_click(_tap(row, label), backend=_owner_backend())

    def _carry_out(self, table: ProcessTable = _CLEAR) -> list[str]:
        with patch("teatree.core.worktree.checkout_attest.read_process_table", return_value=table):
            return carry_out_owner_actions()

    def _outcomes(self, row: DeferredQuestion) -> list[str]:
        return list(
            DeferredQuestionAudit.objects.filter(question=row)
            .exclude(action__in=("answered", "deferred"))
            .values_list("action", flat=True)
        )

    def test_acceptance_attests_and_releases_the_park_once(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")

        assert self._carry_out() == [f"carried out {row.pk}"]
        assert not answer_from_click(_tap(row, "Accept"), backend=_owner_backend())
        assert self._carry_out() == []

        self.task.refresh_from_db()
        self.worktree.refresh_from_db()
        assert self.task.not_before is None
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.checkout).digest
        assert self._outcomes(row) == ["carried_out"]
        assert DeferredQuestionAudit.objects.get(question=row, action="carried_out").resolver_id == f"slack:{_OWNER}"
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=_CLEAR):
            assert claim_task_checkout_or_park(self.task) is None

    def test_decline_keeps_the_task_parked_and_attests_nothing(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Decline")

        assert self._carry_out() == [f"declined {row.pk}"]

        self.task.refresh_from_db()
        self.worktree.refresh_from_db()
        assert self.task.not_before is not None
        assert self.worktree.get_extra()["attested_state"] != checkout_state(self.checkout).digest
        assert self._outcomes(row) == ["declined"]

    def test_nonrecommended_tap_cannot_carry_out_if_decline_shortcut_is_missed(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Decline")

        with patch("teatree.core.answering.owner_actions.owner_declined", return_value=False):
            assert self._carry_out() == [f"refused {row.pk}"]

        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] != checkout_state(self.checkout).digest

    def test_typed_yes_leaves_the_checkout_card_pending(self) -> None:
        row = self._park_on_a_foreign_edit()
        assert not apply_bound_answer(BoundAnswer(row, "yes", answered_by=_OWNER))

        row.refresh_from_db()
        assert row.is_pending
        assert self._carry_out() == []

    def test_verified_typed_replies_leave_the_checkout_card_pending(self) -> None:
        row = self._park_on_a_foreign_edit()
        backend = _owner_backend()
        for index, text in enumerate(("yes", "no", f"#{row.pk} yes"), 1):
            with self.subTest(text=text):
                row.refresh_from_db()
                backend.reset_mock()
                with transaction.atomic():
                    reply = PendingChatInjection.record(
                        channel=row.slack_channel,
                        slack_ts=f"180000000{index}.000100",
                        text=text,
                        context=DmContext(user_id=_OWNER, thread_ts=row.slack_ts),
                    )
                    assert reply is not None
                    backend.fetch_thread_replies.return_value = [
                        {"ts": reply.slack_ts, "thread_ts": row.slack_ts, "user": _OWNER, "text": text}
                    ]

                    AskUserQuestionReplyScanner(backend=backend, reader=_statement).scan()

                    row.refresh_from_db()
                    assert row.is_pending
                    assert self._carry_out() == []
                    backend.fetch_thread_replies.assert_not_called()
                    transaction.set_rollback(True)

    def test_typed_replies_cannot_bind_to_action_card(self) -> None:
        row = self._park_on_a_foreign_edit()
        for index, (text, thread_ts) in enumerate((("Accept", row.slack_ts), ("yes", "")), 1):
            with self.subTest(text=text, thread_ts=thread_ts):
                row.refresh_from_db()
                with transaction.atomic():
                    reply = PendingChatInjection.record(
                        channel=row.slack_channel,
                        slack_ts=f"180000000{index}.000100",
                        text=text,
                        context=DmContext(user_id=_OWNER, thread_ts=thread_ts),
                    )
                    assert reply is not None
                    assert bind_reply(reply, reader=_statement, owner_user_id=_OWNER) is None
                    row.refresh_from_db()
                    assert row.is_pending
                    assert self._carry_out() == []
                    transaction.set_rollback(True)

    def test_an_older_typed_answer_cannot_execute_after_this_fix(self) -> None:
        row = self._park_on_a_foreign_edit()
        assert row.apply_answer("Accept", resolved_via=DeferredQuestion.ResolvedVia.SLACK) is not None
        DeferredQuestionAudit.objects.create(
            question=row,
            action="answered",
            answer_text="Accept",
            resolver_id=f"slack:{_OWNER}",
            note="verified-typed:1800000001.000100",
        )

        assert self._carry_out() == [f"refused {row.pk}"]
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] != checkout_state(self.checkout).digest

    def test_forged_queue_reply_cannot_attest_a_checkout(self) -> None:
        row = self._park_on_a_foreign_edit()
        queue = Path(self.enterContext(TemporaryDirectory())) / "events.jsonl"
        self.enterContext(patch.object(receiver, "default_queue_path", return_value=queue))
        event = {
            "type": "message",
            "channel_type": "im",
            "channel": row.slack_channel,
            "ts": "1800000001.000100",
            "thread_ts": row.slack_ts,
            "user": _OWNER,
            "text": f"#{row.pk} Accept",
        }
        queue.write_text(json.dumps({"overlay": "demo", "event": event}) + "\n")
        backend = _owner_backend()
        backend.fetch_mentions.return_value = []
        backend.auth_test.return_value = {"ok": True, "user_id": "U0DEMOBOT", "bot_id": "B0DEMOBOT"}
        backend.get_permalink.return_value = "https://example.invalid/message"

        SlackMentionsScanner(backend=backend, overlay="demo").scan()
        assert PendingChatInjection.objects.filter(text=event["text"]).exists()
        AskUserQuestionReplyScanner(backend=backend, overlay="demo", reader=_statement).scan()

        row.refresh_from_db()
        assert row.is_pending
        assert self._carry_out() == []
        assert self._outcomes(row) == []
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] != checkout_state(self.checkout).digest

    def test_agent_surface_and_mcp_cannot_answer_an_attest_card(self) -> None:
        row = self._park_on_a_foreign_edit()
        for answer in ("agent", "mcp"):
            if answer == "agent":
                with pytest.raises(SystemExit) as refusal:
                    call_command(
                        "questions",
                        "answer",
                        row.pk,
                        "Accept",
                        "--agent-surface",
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )
                assert refusal.value.code == 2
            else:
                assert _call("question_answer", {"question_id": row.pk, "text": "Accept"})["ok"] is False
            row.refresh_from_db()
            assert row.is_pending
            assert self._carry_out() == []

    def test_policy_answer_cannot_attest_a_checkout(self) -> None:
        row = self._park_on_a_foreign_edit()
        assert auto_answer_by_policy(row, "Accept") is None
        row.refresh_from_db()
        assert row.is_pending
        assert self._carry_out() == []
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] != checkout_state(self.checkout).digest

    def test_typed_text_cannot_resolve_an_action_card(self) -> None:
        row = self._park_on_a_foreign_edit()

        def reader(_text: str) -> InboundReading:
            return InboundReading(InboundIntent.FYI, answerable=False, work_summary="", source=ReadingSource.HEURISTIC)

        for text in ("perhaps later", "Accept."):
            with self.subTest(text=text):
                assert resolve_answer(text, row, reader=reader) is None
                row.refresh_from_db()
                assert row.is_pending

    def test_action_card_and_reminder_require_a_tap(self) -> None:
        row = self._park_on_a_foreign_edit()
        for text in (render_text(row), bump_text(row, now=timezone.now())):
            with self.subTest(text=text):
                assert "Tap an option above" in text
                assert "reply" not in text.lower()

    def test_non_owner_tap_cannot_attest_a_checkout(self) -> None:
        row = self._park_on_a_foreign_edit()
        with self.assertLogs("teatree.loop.question_binding", level="WARNING") as captured:
            assert not answer_from_click(_tap(row, "Accept", user="U0SOMEONEELSE"), backend=_owner_backend())
        row.refresh_from_db()
        assert row.is_pending
        assert self._carry_out() == []
        assert "not the owner" in captured.output[0]

    def test_approval_uses_the_card_option_instead_of_a_fixed_label(self) -> None:
        state = checkout_state(self.checkout)
        card = QuestionCard(
            decision=OwnerDecision.IRREVERSIBLE,
            checked=("The checkout state was inspected.",),
            blocker="Only the owner may accept this state.",
            why="The factory stopped before writing.",
            options=(
                CardOption("Proceed", "I record this checkout state.", recommended=True),
                CardOption("Leave", "The task stays parked."),
            ),
            action=CheckoutAttestation(self.worktree, state, host_evidence=True).action,
        )
        action = card.action
        assert action is not None
        row = DeferredQuestion.record("Should I accept this checkout?", dedupe_marker=action_marker(action), card=card)
        assert answer_from_click(_tap(row, "Proceed"), backend=_owner_backend())

        assert self._carry_out() == [f"carried out {row.pk}"]

    def test_latest_waiting_task_is_released_by_a_reused_card(self) -> None:
        row = self._park_on_a_foreign_edit()
        later = TaskFactory(ticket=self.task.ticket, phase="coding")
        state = checkout_state(self.checkout)
        park_task_checkout(
            later, reason="foreign edit", attestable=CheckoutAttestation(self.worktree, state, host_evidence=True)
        )
        reused = DeferredQuestion.objects.get(pk=row.pk)
        assert reused.pk == row.pk
        self._answer(reused, "Accept")

        assert self._carry_out() == [f"carried out {row.pk}"]
        self.task.refresh_from_db()
        later.refresh_from_db()
        assert self.task.not_before is None
        assert later.not_before is None

    def test_deferred_accept_does_not_create_another_card_on_park_retry(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        busy = ProcessTable(frozenset({self.checkout}), "/proc")
        assert self._carry_out(busy) == []

        state = checkout_state(self.checkout)
        park_task_checkout(
            self.task,
            reason=f"checkout {self.checkout} differs from its attested state",
            attestable=CheckoutAttestation(self.worktree, state, host_evidence=True),
        )

        assert DeferredQuestion.objects.filter(dedupe_marker=row.dedupe_marker).count() == 1
        assert not DeferredQuestion.objects.filter(dedupe_marker=row.dedupe_marker, answered_at__isnull=True).exists()

    def test_accept_only_releases_tasks_parked_for_its_checkout(self) -> None:
        row = self._park_on_a_foreign_edit()
        other_path = Path(self.enterContext(TemporaryDirectory())) / "other"
        _init_repo_with_branch(other_path, branch="feature", commits_ahead=0)
        other_tree = WorktreeFactory(ticket=self.task.ticket, extra={"worktree_path": str(other_path)})
        other_task = TaskFactory(ticket=self.task.ticket, phase="coding")
        park_task_checkout(
            other_task,
            reason=f"checkout {other_path} differs from its attested state",
            attestable=CheckoutAttestation(other_tree, checkout_state(other_path), host_evidence=True),
        )
        self._answer(row, "Accept")

        assert self._carry_out() == [f"carried out {row.pk}"]
        self.task.refresh_from_db()
        other_task.refresh_from_db()
        assert self.task.not_before is None
        assert other_task.not_before is not None

    def test_answered_action_can_be_asked_again_for_the_same_state(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        assert self._carry_out() == [f"carried out {row.pk}"]
        state = checkout_state(self.checkout)

        renewed = ask_owner_to_attest(self.task, CheckoutAttestation(self.worktree, state, host_evidence=True))

        assert renewed is not None
        assert renewed.pk != row.pk
        assert renewed.is_pending

    def test_new_task_can_ask_before_and_after_a_decline_is_settled(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Decline")
        later = TaskFactory(ticket=self.task.ticket, phase="coding")
        state = checkout_state(self.checkout)

        renewed = ask_owner_to_attest(later, CheckoutAttestation(self.worktree, state, host_evidence=True))

        assert renewed is not None
        with self.subTest(stage="before_outcome"):
            assert renewed.pk != row.pk
            assert renewed.parked_task_id == later.pk
            assert renewed.is_pending
        renewed.mark_stale("new task will ask after the declined outcome")
        assert self._carry_out() == [f"declined {row.pk}"]
        after_outcome = TaskFactory(ticket=self.task.ticket, phase="coding")
        again = ask_owner_to_attest(after_outcome, CheckoutAttestation(self.worktree, state, host_evidence=True))

        with self.subTest(stage="after_outcome"):
            assert again is not None
            assert again.pk not in {row.pk, renewed.pk}
            assert again.parked_task_id == after_outcome.pk
            assert again.is_pending

    def test_typed_accept_holds_the_card_for_a_new_task_and_never_attests(self) -> None:
        row = self._park_on_a_foreign_edit()
        assert row.apply_answer("Accept", resolved_via=DeferredQuestion.ResolvedVia.SLACK) is not None
        later = TaskFactory(ticket=self.task.ticket, phase="coding")

        held = ask_owner_to_attest(
            later, CheckoutAttestation(self.worktree, checkout_state(self.checkout), host_evidence=True)
        )

        assert held is not None
        assert held.pk == row.pk
        assert DeferredQuestion.objects.filter(dedupe_marker=row.dedupe_marker).count() == 1
        assert self._carry_out() == [f"refused {row.pk}"]

    def test_card_without_host_evidence_keeps_its_one_run_binding(self) -> None:
        unavailable = ProcessTable(frozenset(), "")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=unavailable):
            assert claim_task_checkout_or_park(self.task) is not None
        row = self.task.deferred_questions.get()
        self._answer(row, "Accept")

        assert self._carry_out() == [f"carried out {row.pk}"]
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == f"owner:{checkout_state(self.checkout).digest}"

    def test_new_digest_supersedes_an_old_pending_card(self) -> None:
        old = self._park_on_a_foreign_edit()
        (self.checkout / "new.txt").write_text("new foreign edit")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=_CLEAR):
            assert claim_task_checkout_or_park(self.task) is not None

        old.refresh_from_db()
        assert old.dismissed_at is not None
        assert self.task.deferred_questions.filter(answered_at__isnull=True, dismissed_at__isnull=True).count() == 1

    def test_a_checkout_changed_after_the_card_is_refused_with_one_notice(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        (self.checkout / "new.txt").write_text("changed again")

        with patch("teatree.core.answering.owner_actions.notify_user") as notice:
            assert self._carry_out() == [f"refused {row.pk}"]

        notice.assert_called_once()
        assert row.question in notice.call_args.args[0]
        assert self.task.ticket.issue_url in notice.call_args.args[0]
        self.task.refresh_from_db()
        assert self.task.not_before is not None
        assert self._outcomes(row) == ["refused"]

    def test_a_checkout_changed_after_preparation_is_not_attested(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        reads = 0

        def move_after_first_read(
            worktree: Worktree, *, expected_digest: str, requested_host_evidence: bool | None
        ) -> CheckoutAttestation:
            nonlocal reads
            attestation = read_attestation(
                worktree, expected_digest=expected_digest, requested_host_evidence=requested_host_evidence
            )
            reads += 1
            if reads == 1:
                (self.checkout / "new.txt").write_text("changed during approval")
            return attestation

        with (
            patch("teatree.core.answering.owner_actions.read_attestation", side_effect=move_after_first_read),
            patch("teatree.core.worktree.checkout_attest.read_process_table", return_value=_CLEAR),
        ):
            assert carry_out_owner_actions() == [f"refused {row.pk}"]

        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] != checkout_state(self.checkout).digest
        assert self._outcomes(row) == ["refused"]

    def test_a_busy_checkout_waits_then_attests_once_the_process_ends(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")

        assert self._carry_out(ProcessTable(frozenset({self.checkout}), "/proc")) == []
        assert self._outcomes(row) == []
        DeferredQuestionAudit.objects.filter(question=row, action="deferred").update(
            resolved_at=timezone.now() - timedelta(minutes=6)
        )

        assert self._carry_out() == [f"carried out {row.pk}"]

    def test_deferred_action_backs_off_and_notifies_owner(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        busy = ProcessTable(frozenset({self.checkout}), "/proc")

        with patch("teatree.core.answering.owner_actions.notify_user") as notice:
            assert self._carry_out(busy) == []
            assert self._carry_out(busy) == []

        assert DeferredQuestionAudit.objects.filter(question=row, action="deferred").count() == 1
        notice.assert_called_once()

    def test_deferred_action_expires_with_a_visible_refusal(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        busy = ProcessTable(frozenset({self.checkout}), "/proc")

        with patch("teatree.core.answering.owner_actions.notify_user") as notice:
            assert self._carry_out(busy) == []
            DeferredQuestionAudit.objects.filter(question=row, action="deferred").update(
                resolved_at=timezone.now() - timedelta(hours=25)
            )
            assert self._carry_out(busy) == [f"refused {row.pk}"]

        assert self._outcomes(row) == ["refused"]
        assert notice.call_count == 2

    def test_a_day_old_answer_is_tried_before_it_can_expire(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        DeferredQuestion.objects.filter(pk=row.pk).update(answered_at=timezone.now() - timedelta(hours=25))

        assert self._carry_out() == [f"carried out {row.pk}"]

    def test_the_checkout_is_rechecked_under_the_write_lock(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        depths: list[int] = []

        def read_table(**_kwargs: object) -> ProcessTable:
            depths.append(len(connection.atomic_blocks))
            return _CLEAR

        baseline = len(connection.atomic_blocks)
        with patch("teatree.core.worktree.checkout_attest.read_process_table", side_effect=read_table):
            assert carry_out_owner_actions() == [f"carried out {row.pk}"]

        assert depths == [baseline, baseline + 1]

    def test_acceptance_does_not_release_a_later_usage_park(self) -> None:
        row = self._park_on_a_foreign_edit()
        later = timezone.now() + timedelta(hours=1)
        TaskAttempt.objects.create(
            task=self.task,
            ended_at=timezone.now(),
            exit_code=0,
            error="limit_parked: subscription_session: window",
            result={"summary": "limit_parked: subscription_session: window"},
        )
        self.task.park(not_before=later)
        self._answer(row, "Accept")

        assert self._carry_out() == [f"carried out {row.pk}"]

        self.task.refresh_from_db()
        assert self.task.not_before == later

    def test_a_card_that_errors_does_not_hold_back_the_next(self) -> None:
        landed_ticket()
        with stub_forge():
            void_card = request_void(PR_URL, "The merge content disappeared.")
        assert void_card.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000200")
        assert answer_from_click(_tap(void_card, "Void"), backend=_owner_backend())
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")

        with (
            stub_forge(TypeError("malformed forge payload")),
            self.assertLogs("teatree.core.answering.owner_actions", "ERROR"),
        ):
            assert self._carry_out() == [f"carried out {row.pk}"]

        assert self._outcomes(void_card) == []
        assert DeferredQuestionAudit.objects.filter(question=void_card, action="deferred").count() == 1
        DeferredQuestionAudit.objects.filter(question=void_card, action="deferred").update(
            resolved_at=timezone.now() - timedelta(hours=25)
        )
        with (
            stub_forge(TypeError("malformed forge payload")),
            patch("teatree.core.answering.owner_actions.notify_user") as notice,
            self.assertLogs("teatree.core.answering.owner_actions", "ERROR"),
        ):
            assert self._carry_out() == [f"refused {void_card.pk}"]
        notice.assert_called_once()
        assert self._outcomes(void_card) == ["refused"]


class TestActionIdentity(TestCase):
    def test_a_mismatched_marker_refuses_the_action_once(self) -> None:
        action = ActionRequest(kind=OwnerActionKind.ATTEST, target="1", binding="digest")
        card = QuestionCard(
            decision=OwnerDecision.IRREVERSIBLE,
            checked=("The checkout changed.",),
            blocker="Only the owner may accept it.",
            why="The factory stopped before writing.",
            options=(
                CardOption("Accept", "I record this state.", recommended=True),
                CardOption("Decline", "Nothing changes."),
            ),
            action=action,
        )
        row = DeferredQuestion.record("Should I accept the checkout?", dedupe_marker=action_marker(action), card=card)
        assert row.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000300")
        assert answer_from_click(_tap(row, "Accept"), backend=_owner_backend())
        DeferredQuestion.objects.filter(pk=row.pk).update(dedupe_marker="other")

        assert carry_out_owner_actions() == [f"refused {row.pk}"]
        assert carry_out_owner_actions() == []
        assert DeferredQuestionAudit.objects.get(question=row, action="refused").note == (
            "The action card no longer matches its recorded request."
        )
