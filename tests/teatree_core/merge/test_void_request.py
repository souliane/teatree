"""Owner card flow for a recorded merge whose content is absent."""

from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TestCase

from teatree.core.answering.owner_actions import carry_out_owner_actions
from teatree.core.evidence.forge_content import PrContentSnapshot
from teatree.core.merge.void_merge import ApprovedVoid, approve_owner_void
from teatree.core.merge.void_request import request_void
from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models import DeferredQuestion, DmContext, MergeVoid, PendingChatInjection, PullRequest, Ticket
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.question_binding import answer_from_click
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
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

    def test_non_text_legacy_title_uses_a_safe_fallback(self) -> None:
        self.ticket.short_description = ""
        self.ticket.extra = {"issue_title": {"private": "data"}}
        self.ticket.save(update_fields=["short_description", "extra"])

        with stub_forge():
            row = self._ask()

        assert "this pull request" in row.question
        assert "private" not in row.question

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

    def test_ledger_change_after_approval_refuses_stale_void_plan(self) -> None:
        def move_ledger(pr_url: str, *, binding: str, reason: str, actor: str) -> ApprovedVoid:
            approved = approve_owner_void(pr_url, binding=binding, reason=reason, actor=actor)
            Ticket.objects.filter(pk=self.ticket.pk).update(state=Ticket.State.CODED)
            return approved

        with stub_forge(), patch("teatree.core.answering.owner_actions.approve_owner_void", side_effect=move_ledger):
            row = self._ask()
            assert answer_from_click(_tap("Void", 1), backend=self.backend)
            assert carry_out_owner_actions() == [f"refused {row.pk}"]

        assert not MergeVoid.objects.exists()

    def test_ticket_change_before_void_tap_refuses_stale_card(self) -> None:
        with stub_forge():
            row = self._ask()
            Ticket.objects.filter(pk=self.ticket.pk).update(state=Ticket.State.CODED)
            assert answer_from_click(_tap("Void", 1), backend=self.backend)
            assert carry_out_owner_actions() == [f"refused {row.pk}"]

        assert not MergeVoid.objects.exists()
        assert PullRequest.objects.get(ticket=self.ticket).state == PullRequest.State.MERGED

    def test_verified_typed_reply_cannot_void(self) -> None:
        with stub_forge():
            row = self._ask()
            reply = PendingChatInjection.record(
                channel=row.slack_channel,
                slack_ts="1800000001.000100",
                text="go ahead",
                context=DmContext(user_id=_OWNER, thread_ts=row.slack_ts),
            )
            assert reply is not None
            self.backend.fetch_thread_replies.return_value = [
                {"ts": reply.slack_ts, "thread_ts": row.slack_ts, "user": _OWNER, "text": reply.text}
            ]
            self.backend.get_permalink.return_value = "https://example.invalid/reply"

            def reader(_text: str) -> InboundReading:
                return InboundReading(
                    InboundIntent.FYI, answerable=False, work_summary="", source=ReadingSource.HEURISTIC
                )

            AskUserQuestionReplyScanner(backend=self.backend, reader=reader).scan()
            row.refresh_from_db()
            assert row.is_pending
            assert carry_out_owner_actions() == []

        assert not MergeVoid.objects.exists()

    def test_keep_records_a_decline_and_changes_nothing(self) -> None:
        with stub_forge():
            row = self._ask()
            assert answer_from_click(_tap("Keep", 2), backend=self.backend)
            assert carry_out_owner_actions() == [f"declined {row.pk}"]
            assert carry_out_owner_actions() == []

        assert not MergeVoid.objects.exists()
        self.ticket.refresh_from_db()
        assert self.ticket.state == Ticket.State.MERGED

    def test_keep_can_be_asked_again_before_the_outcome_worker_runs(self) -> None:
        with stub_forge():
            row = self._ask()
            assert answer_from_click(_tap("Keep", 2), backend=self.backend)
            renewed = request_void(PR_URL, "The merge content disappeared.")

        assert renewed.pk != row.pk
        assert renewed.is_pending
        assert not MergeVoid.objects.exists()

    def test_typed_void_cannot_settle_the_card(self) -> None:
        with stub_forge():
            row = self._ask()
            assert row.apply_answer("Void", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
            card = QuestionCard.from_row(row.evidence, row.options_json)
            assert card is not None
            held = DeferredQuestion.record(row.question, dedupe_marker=row.dedupe_marker, card=card)

        assert held.pk == row.pk
        assert DeferredQuestion.objects.filter(dedupe_marker=row.dedupe_marker).count() == 1

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
        assert len(captured.output) == 1
        assert "1800000000.000100" in captured.output[0]
        assert "U0SOMEONEELSE" not in captured.output[0]

    def test_long_legacy_title_keeps_the_request_answerable(self) -> None:
        self.ticket.short_description = ""
        self.ticket.extra = {**self.ticket.extra, "issue_title": "A merge title that is much too long " * 8}
        self.ticket.save(update_fields=["short_description", "extra"])

        with stub_forge():
            row = self._ask()

        assert "A merge title" in row.question
