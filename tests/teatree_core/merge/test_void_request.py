"""Owner card flow for a recorded merge whose content is absent."""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.merge.void_request import request_void
from teatree.core.models import DeferredQuestion, MergeVoid, PullRequest, Ticket
from teatree.core.owner_actions import carry_out_owner_actions
from teatree.loop.question_binding import answer_from_click
from tests.void_merge_fixtures import PR_URL, landed_ticket, stub_forge

_OWNER = "U0DEMOOWNER"


def _tap(label: str, index: int, *, user: str = _OWNER) -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": "D0DEMOOWNER"},
        "message": {"ts": "1800000000.000100"},
        "actions": [
            {
                "action_id": f"owner-question-option-{index}",
                "value": str(index),
                "text": {"text": label},
            }
        ],
    }


class TestVoidRequest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = landed_ticket()
        self.backend = MagicMock(user_id=_OWNER)
        self.backend.update_message.return_value = {"ok": True}

    def _ask(self) -> DeferredQuestion:
        row = request_void(PR_URL, "The merge content disappeared.")
        row.slack_channel = "D0DEMOOWNER"
        row.slack_ts = "1800000000.000100"
        row.save(update_fields=["slack_channel", "slack_ts"])
        return row

    def test_owner_tap_voids_once_and_records_the_answering_identity(self) -> None:
        with stub_forge():
            row = self._ask()
            assert answer_from_click(_tap("Void", 1), backend=self.backend)
            assert carry_out_owner_actions() == [f"carried out {row.pk}"]
            assert not answer_from_click(_tap("Void", 1), backend=self.backend)
            assert carry_out_owner_actions() == []

        void = MergeVoid.objects.get()
        assert void.actor == f"slack:{_OWNER}"
        self.ticket.refresh_from_db()
        assert self.ticket.state == Ticket.State.CODED
        assert PullRequest.objects.get(ticket=self.ticket).state == PullRequest.State.CLOSED

    def test_keep_records_a_decline_and_changes_nothing(self) -> None:
        with stub_forge():
            row = self._ask()
            assert answer_from_click(_tap("Keep", 2), backend=self.backend)
            assert carry_out_owner_actions() == [f"declined {row.pk}"]
            assert carry_out_owner_actions() == []

        assert not MergeVoid.objects.exists()
        self.ticket.refresh_from_db()
        assert self.ticket.state == Ticket.State.MERGED

    def test_local_answer_cannot_authorize_the_action(self) -> None:
        with stub_forge():
            row = self._ask()
            assert row.apply_answer("Void", resolved_via=DeferredQuestion.ResolvedVia.LOCAL)
            assert carry_out_owner_actions() == [f"refused {row.pk}"]
        assert not MergeVoid.objects.exists()

    def test_non_owner_tap_is_logged_and_leaves_the_card_pending(self) -> None:
        with stub_forge():
            row = self._ask()
            with self.assertLogs("teatree.loop.question_binding", level="WARNING") as captured:
                assert not answer_from_click(_tap("Void", 1, user="U0SOMEONEELSE"), backend=self.backend)
        row.refresh_from_db()
        assert row.is_pending
        assert "1800000000.000100" in captured.output[0]
        assert "U0SOMEONEELSE" not in captured.output[0]
