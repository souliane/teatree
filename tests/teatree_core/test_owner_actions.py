"""An owner's Slack answer carries out its action once, after rechecking the live evidence outside the write lock."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models import DeferredQuestion, DeferredQuestionAudit, TaskAttempt
from teatree.core.owner_actions import carry_out_owner_actions
from teatree.core.worktree.checkout_fence import claim_task_checkout_or_park, verify_task_checkout
from teatree.core.worktree.checkout_state import checkout_state
from teatree.loop.question_binding import BoundAnswer, answer_from_click, apply_bound_answer
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch

_OWNER = "U0DEMOOWNER"
_CLEAR = ProcessTable(frozenset(), "/proc")
_OPTION_INDEX = {"Accept": 1, "Decline": 2}


def _tap(row: DeferredQuestion, label: str) -> dict[str, object]:
    index = _OPTION_INDEX[label]
    return {
        "type": "block_actions",
        "user": {"id": _OWNER},
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
            .exclude(action="answered")
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

    def test_a_checkout_changed_after_the_card_is_refused_with_one_notice(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        (self.checkout / "new.txt").write_text("changed again")

        with patch("teatree.core.owner_actions.notify_user") as notice:
            assert self._carry_out() == [f"refused {row.pk}"]

        notice.assert_called_once()
        self.task.refresh_from_db()
        assert self.task.not_before is not None
        assert self._outcomes(row) == ["refused"]

    def test_a_busy_checkout_waits_then_attests_once_the_process_ends(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")

        assert self._carry_out(ProcessTable(frozenset({self.checkout}), "/proc")) == []
        assert self._outcomes(row) == []

        assert self._carry_out() == [f"carried out {row.pk}"]

    def test_the_checkout_is_read_outside_the_write_lock(self) -> None:
        row = self._park_on_a_foreign_edit()
        self._answer(row, "Accept")
        depths: list[int] = []

        def read_table(**_kwargs: object) -> ProcessTable:
            depths.append(len(connection.atomic_blocks))
            return _CLEAR

        baseline = len(connection.atomic_blocks)
        with patch("teatree.core.worktree.checkout_attest.read_process_table", side_effect=read_table):
            assert carry_out_owner_actions() == [f"carried out {row.pk}"]

        assert depths == [baseline]

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
        row = DeferredQuestion.record("Should I accept the checkout?", dedupe_marker="other", card=card)
        assert apply_bound_answer(BoundAnswer(row, "Accept", answered_by=_OWNER))

        assert carry_out_owner_actions() == [f"refused {row.pk}"]
        assert carry_out_owner_actions() == []
        assert DeferredQuestionAudit.objects.filter(question=row, action="refused").count() == 1
