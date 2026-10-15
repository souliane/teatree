"""Owner card flow for a recorded merge whose content is absent."""

from unittest.mock import MagicMock

from django.db import connection
from django.test import TestCase

from teatree.core.answering.owner_actions import carry_out_owner_actions
from teatree.core.evidence.forge_content import PrContentSnapshot
from teatree.core.merge.void_request import request_void
from teatree.core.models import DeferredQuestion, MergeVoid, PullRequest, Ticket
from teatree.loop.question_binding import answer_from_click
from tests.void_merge_fixtures import ABSENT, PR_URL, landed_ticket, stub_forge

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

    def test_the_card_carries_the_forge_evidence_and_the_reason(self) -> None:
        with stub_forge():
            row = self._ask()

        assert row.question.startswith(f"Should I void the recorded merge of <{PR_URL}|")
        assert row.evidence["checked"] == [
            "The forge records this pull request as merged.",
            "The default branch holds none of its 2 changed files.",
            "It returns 1 ticket to the state before the merge.",
            "It closes 1 pull request record.",
        ]
        assert row.evidence["quoted"] == "The merge content disappeared."

    def test_the_forge_is_read_outside_the_write_lock(self) -> None:
        depths: list[int] = []

        def snapshot() -> PrContentSnapshot:
            depths.append(len(connection.atomic_blocks))
            return ABSENT

        with stub_forge() as forge:
            row = self._ask()
            assert answer_from_click(_tap("Void", 1), backend=self.backend)
            forge.side_effect = snapshot
            baseline = len(connection.atomic_blocks)
            assert carry_out_owner_actions() == [f"carried out {row.pk}"]

        assert depths == [baseline]

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

    def test_long_legacy_title_keeps_the_request_answerable(self) -> None:
        self.ticket.short_description = ""
        self.ticket.extra = {**self.ticket.extra, "issue_title": "A merge title that is much too long " * 8}
        self.ticket.save(update_fields=["short_description", "extra"])

        with stub_forge():
            row = self._ask()

        assert "A merge title" in row.question
