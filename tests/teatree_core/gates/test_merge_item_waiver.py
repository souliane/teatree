"""An owner's recorded waiver turns one LLM merge item from unmet to met, for that ticket only."""

from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase

from teatree.core.answering.owner_actions import carry_out_owner_actions
from teatree.core.gates.merge_item_waiver import MergeItemWaiverError, request_merge_item_waiver, waive_merge_item
from teatree.core.gates.merge_quality_gate import MergeQualityVerdictError, check_merge_quality_verdict, unmet_items
from teatree.core.models import CriticFinding, CriticVerdict, DeferredQuestion, Ticket
from teatree.core.models.types import validated_ticket_extra
from teatree.core.owner_action_auth import direct_owner_action
from teatree.loop.question_binding import answer_from_click

_HEAD = "a" * 40
_PASS = {"slug": "cleanliness", "status": "pass", "citation": "src/x.py:1 clean"}
_FAIL = {"slug": "test_value", "status": "fail", "citation": "REDUNDANT tests/a.py::t — pinned by tests/b.py::t"}


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)


def _verdict(ticket: Ticket, *items: dict[str, str]) -> CriticVerdict:
    return CriticVerdict.objects.create(
        ticket=ticket, transition="merge", head_sha=_HEAD, grader_identity="critic-agent-7", items=list(items)
    )


def _fresh(verdict: CriticVerdict) -> CriticVerdict:
    return CriticVerdict.objects.get(pk=verdict.pk)


def _approved_question(ticket: Ticket, *, slug: str, reason: str) -> DeferredQuestion:
    question = request_merge_item_waiver(ticket, slug=slug, reason=reason)
    assert question.mark_mirrored(channel="D0DEMOOWNER", slack_ts=f"1800000000.{question.pk:06d}")
    backend = MagicMock(user_id="U0DEMOOWNER")
    backend.update_message.return_value = {"ok": True}
    click = {
        "type": "block_actions",
        "user": {"id": "U0DEMOOWNER"},
        "channel": {"id": question.slack_channel},
        "message": {"ts": question.slack_ts},
        "actions": [{"action_id": "owner-question-option-1", "value": "1", "text": {"text": "Accept"}}],
    }
    assert answer_from_click(click, backend=backend)
    return question


def _waive_as_owner(ticket: Ticket, *, slug: str, reason: str) -> dict[str, str]:
    question = _approved_question(ticket, slug=slug, reason=reason)
    return waive_merge_item(ticket, slug=slug, waived_by="slack:U0DEMOOWNER", reason=reason, owner_question=question)


class TestAWaivedItemIsNotUnmet(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = _ticket()
        self.verdict = _verdict(self.ticket, _FAIL, _PASS)

    def test_without_a_waiver_the_failed_item_is_unmet(self) -> None:
        assert unmet_items(_fresh(self.verdict)) == ["test_value"]

    def test_an_owner_waiver_makes_it_met(self) -> None:
        _waive_as_owner(self.ticket, slug="test_value", reason="three rounds on the same test")

        assert unmet_items(_fresh(self.verdict)) == []

    def test_the_waiver_records_who_why_and_when_and_survives_extra_validation(self) -> None:
        _waive_as_owner(self.ticket, slug="test_value", reason="three rounds on the same test")

        self.ticket.refresh_from_db()
        waiver = validated_ticket_extra(self.ticket.extra)["merge_item_waivers"]["test_value"]
        assert (waiver["waived_by"], waiver["reason"]) == ("slack:U0DEMOOWNER", "three rounds on the same test")
        assert waiver["waived_at"]

    def test_replaying_the_same_owner_answer_keeps_the_first_waiver(self) -> None:
        question = _approved_question(self.ticket, slug="test_value", reason="first reason")
        first = waive_merge_item(
            self.ticket,
            slug="test_value",
            waived_by="slack:U0DEMOOWNER",
            reason="first reason",
            owner_question=question,
        )
        replay = waive_merge_item(
            self.ticket,
            slug="test_value",
            waived_by="slack:U0DEMOOWNER",
            reason="first reason",
            owner_question=question,
        )

        self.ticket.refresh_from_db()
        assert replay == first
        assert self.ticket.extra["merge_item_waivers"]["test_value"]["reason"] == "first reason"

    def test_it_does_not_apply_to_another_ticket(self) -> None:
        other = _ticket()
        other_verdict = _verdict(other, _FAIL, _PASS)
        _waive_as_owner(self.ticket, slug="test_value", reason="three rounds on the same test")

        assert unmet_items(_fresh(other_verdict)) == ["test_value"]

    def test_the_other_item_stays_unmet(self) -> None:
        _waive_as_owner(self.ticket, slug="cleanliness", reason="accepted as is")

        assert unmet_items(_fresh(self.verdict)) == ["test_value"]


class TestFirstWaiverTransaction(TransactionTestCase):
    def test_the_first_waiver_check_and_merge_share_a_transaction(self) -> None:
        ticket = _ticket()
        atomic_at_merge: list[bool] = []
        original_merge = Ticket.merge_extra

        def observe_merge(row: Ticket, *, merge_into_dicts: dict[str, dict[str, str]]) -> None:
            atomic_at_merge.append(connection.in_atomic_block)
            original_merge(row, merge_into_dicts=merge_into_dicts)

        question = _approved_question(ticket, slug="test_value", reason="first reason")
        with patch.object(Ticket, "merge_extra", observe_merge):
            waive_merge_item(
                ticket, slug="test_value", waived_by="slack:U0DEMOOWNER", reason="first reason", owner_question=question
            )

        assert atomic_at_merge == [True]


class TestAWaiverIsRefused(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = _ticket()

    def _assert_refused(
        self,
        *,
        slug: str = "test_value",
        waived_by: str = "owner",
        reason: str = "accepted",
        owner_question: DeferredQuestion | None = None,
    ) -> None:
        with pytest.raises(MergeItemWaiverError):
            waive_merge_item(self.ticket, slug=slug, waived_by=waived_by, reason=reason, owner_question=owner_question)
        self.ticket.refresh_from_db()
        assert not self.ticket.extra.get("merge_item_waivers")

    def test_for_an_item_outside_the_llm_merge_items(self) -> None:
        for slug in ("done_not_done", "spec_not_plan", "completeness", "not_a_slug", ""):
            with self.subTest(slug=slug):
                self._assert_refused(slug=slug)

    def test_for_an_agent_identity(self) -> None:
        question = _approved_question(self.ticket, slug="test_value", reason="accepted")
        for identity in ("coding-agent", "loop", "critic-agent-7", "unknown-agent", ""):
            with self.subTest(identity=identity):
                self._assert_refused(waived_by=identity, owner_question=question)

    def test_without_a_reason(self) -> None:
        self._assert_refused(reason="  ")

    def test_a_terminal_and_configured_alias_do_not_prove_owner_identity(self) -> None:
        with (
            patch("teatree.config.get_effective_settings", return_value=MagicMock(user_identity_aliases=["owner"])),
            patch("teatree.core.owner_action_auth.sys.stdin.isatty", return_value=True),
            patch.dict("os.environ", {}, clear=True),
        ):
            assert direct_owner_action()
            self._assert_refused(waived_by="owner")

    def test_a_proven_answer_cannot_be_claimed_by_an_alias(self) -> None:
        question = _approved_question(self.ticket, slug="test_value", reason="accepted")
        with pytest.raises(MergeItemWaiverError, match="identity does not match"):
            waive_merge_item(
                self.ticket, slug="test_value", waived_by="owner", reason="accepted", owner_question=question
            )
        self.ticket.refresh_from_db()
        assert not self.ticket.extra.get("merge_item_waivers")


class TestAWaivedItemLeavesNoFailFinding(TestCase):
    def test_the_stale_finding_is_removed_and_the_merge_check_passes(self) -> None:
        ticket = _ticket()
        _verdict(ticket, _FAIL, _PASS)
        with pytest.raises(MergeQualityVerdictError):
            check_merge_quality_verdict(ticket, _HEAD)
        assert CriticFinding.objects.filter(ticket=ticket, transition="merge", rubric_item="test_value").exists()

        _waive_as_owner(ticket, slug="test_value", reason="three rounds on the same test")
        check_merge_quality_verdict(ticket, _HEAD)

        assert not CriticFinding.objects.filter(ticket=ticket, transition="merge").exists()


class TestTheMergeItemWaiveCommand(TestCase):
    def test_a_direct_terminal_command_requests_a_proven_owner_answer(self) -> None:
        ticket = _ticket()
        verdict = _verdict(ticket, _FAIL, _PASS)

        with (
            patch("teatree.core.owner_action_auth.sys.stdin.isatty", return_value=True),
            patch.dict("os.environ", {}, clear=True),
        ):
            assert direct_owner_action()
            call_command(
                "ticket",
                "merge-item-waive",
                str(ticket.pk),
                "--item",
                "test_value",
                "--waived-by",
                "owner",
                "--reason",
                "three rounds on the same test",
            )

        assert DeferredQuestion.objects.filter(audience=DeferredQuestion.Audience.OWNER_QUESTION).count() == 1
        assert unmet_items(_fresh(verdict)) == ["test_value"]

    def test_an_agent_command_queues_an_owner_question_and_the_owner_tap_records_once(self) -> None:
        ticket = _ticket()
        call_command(
            "ticket",
            "merge-item-waive",
            str(ticket.pk),
            "--item",
            "test_value",
            "--waived-by",
            "owner",
            "--reason",
            "three rounds on the same test",
        )
        ticket.refresh_from_db()
        assert not ticket.extra.get("merge_item_waivers")
        row = DeferredQuestion.objects.get()
        assert row.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000300")
        backend = MagicMock(user_id="U0DEMOOWNER")
        backend.update_message.return_value = {"ok": True}
        click = {
            "type": "block_actions",
            "user": {"id": "U0DEMOOWNER"},
            "channel": {"id": row.slack_channel},
            "message": {"ts": row.slack_ts},
            "actions": [{"action_id": "owner-question-option-1", "value": "1", "text": {"text": "Accept"}}],
        }
        assert answer_from_click(click, backend=backend)
        assert carry_out_owner_actions() == [f"carried out {row.pk}"]
        assert carry_out_owner_actions() == []
        ticket.refresh_from_db()
        assert ticket.extra["merge_item_waivers"]["test_value"]["waived_by"] == "slack:U0DEMOOWNER"

    def test_a_refusal_exits_nonzero_and_records_nothing(self) -> None:
        ticket = _ticket()

        with pytest.raises(SystemExit) as exit_info:
            call_command(
                "ticket",
                "merge-item-waive",
                str(ticket.pk),
                "--item",
                "done_not_done",
                "--waived-by",
                "owner",
                "--reason",
                "accepted",
            )

        ticket.refresh_from_db()
        assert exit_info.value.code == 1
        assert not ticket.extra.get("merge_item_waivers")
