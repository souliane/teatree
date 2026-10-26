"""An owner's recorded waiver turns one LLM merge item from unmet to met, for that ticket only."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.gates.merge_item_waiver import MergeItemWaiverError, waive_merge_item
from teatree.core.gates.merge_quality_gate import MergeQualityVerdictError, check_merge_quality_verdict, unmet_items
from teatree.core.models import CriticFinding, CriticVerdict, Ticket
from teatree.core.models.types import validated_ticket_extra

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


class _ConfiguredOwner(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(
            patch(
                "teatree.config.get_effective_settings", return_value=SimpleNamespace(user_identity_aliases=["owner"])
            )
        )


class TestAWaivedItemIsNotUnmet(_ConfiguredOwner):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = _ticket()
        self.verdict = _verdict(self.ticket, _FAIL, _PASS)

    def test_without_a_waiver_the_failed_item_is_unmet(self) -> None:
        assert unmet_items(_fresh(self.verdict)) == ["test_value"]

    def test_an_owner_waiver_makes_it_met(self) -> None:
        waive_merge_item(self.ticket, slug="test_value", waived_by=" OWNER ", reason="three rounds on the same test")

        assert unmet_items(_fresh(self.verdict)) == []

    def test_the_waiver_records_who_why_and_when_and_survives_extra_validation(self) -> None:
        waive_merge_item(self.ticket, slug="test_value", waived_by="owner", reason="three rounds on the same test")

        self.ticket.refresh_from_db()
        waiver = validated_ticket_extra(self.ticket.extra)["merge_item_waivers"]["test_value"]
        assert (waiver["waived_by"], waiver["reason"]) == ("owner", "three rounds on the same test")
        assert waiver["waived_at"]

    def test_a_second_waiver_for_the_same_item_keeps_the_first(self) -> None:
        waive_merge_item(self.ticket, slug="test_value", waived_by="owner", reason="first reason")
        waive_merge_item(self.ticket, slug="test_value", waived_by="owner", reason="second reason")

        self.ticket.refresh_from_db()
        assert self.ticket.extra["merge_item_waivers"]["test_value"]["reason"] == "first reason"

    def test_it_does_not_apply_to_another_ticket(self) -> None:
        other = _ticket()
        other_verdict = _verdict(other, _FAIL, _PASS)
        waive_merge_item(self.ticket, slug="test_value", waived_by="owner", reason="three rounds on the same test")

        assert unmet_items(_fresh(other_verdict)) == ["test_value"]

    def test_the_other_item_stays_unmet(self) -> None:
        waive_merge_item(self.ticket, slug="cleanliness", waived_by="owner", reason="accepted as is")

        assert unmet_items(_fresh(self.verdict)) == ["test_value"]

    def test_the_ticket_overlay_owns_the_identity_scope(self) -> None:
        def owner_settings(overlay: str | None = None) -> SimpleNamespace:
            aliases = ["overlay-owner"] if overlay == self.ticket.overlay else ["global-owner"]
            return SimpleNamespace(user_identity_aliases=aliases)

        with patch("teatree.config.get_effective_settings", side_effect=owner_settings):
            with pytest.raises(MergeItemWaiverError):
                waive_merge_item(self.ticket, slug="test_value", waived_by="global-owner", reason="wrong owner")
            waive_merge_item(self.ticket, slug="test_value", waived_by="overlay-owner", reason="accepted")

        assert unmet_items(_fresh(self.verdict)) == []


class TestAWaiverIsRefused(_ConfiguredOwner):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = _ticket()

    def _assert_refused(self, *, slug: str = "test_value", waived_by: str = "owner", reason: str = "accepted") -> None:
        with pytest.raises(MergeItemWaiverError):
            waive_merge_item(self.ticket, slug=slug, waived_by=waived_by, reason=reason)
        self.ticket.refresh_from_db()
        assert not self.ticket.extra.get("merge_item_waivers")

    def test_for_an_item_outside_the_llm_merge_items(self) -> None:
        for slug in ("done_not_done", "spec_not_plan", "completeness", "not_a_slug", ""):
            with self.subTest(slug=slug):
                self._assert_refused(slug=slug)

    def test_for_an_agent_identity(self) -> None:
        for identity in ("coding-agent", "loop", "critic-agent-7", "unknown-agent", ""):
            with self.subTest(identity=identity):
                self._assert_refused(waived_by=identity)

    def test_without_a_reason(self) -> None:
        self._assert_refused(reason="  ")

    def test_without_a_configured_owner_alias(self) -> None:
        with patch("teatree.config.get_effective_settings", return_value=SimpleNamespace(user_identity_aliases=[])):
            self._assert_refused()

    def test_when_owner_aliases_cannot_be_read(self) -> None:
        with patch("teatree.config.get_effective_settings", side_effect=RuntimeError("control database locked")):
            self._assert_refused()


class TestAWaivedItemLeavesNoFailFinding(_ConfiguredOwner):
    def test_the_stale_finding_is_removed_and_the_merge_check_passes(self) -> None:
        ticket = _ticket()
        _verdict(ticket, _FAIL, _PASS)
        with pytest.raises(MergeQualityVerdictError):
            check_merge_quality_verdict(ticket, _HEAD)
        assert CriticFinding.objects.filter(ticket=ticket, transition="merge", rubric_item="test_value").exists()

        waive_merge_item(ticket, slug="test_value", waived_by="owner", reason="three rounds on the same test")
        check_merge_quality_verdict(ticket, _HEAD)

        assert not CriticFinding.objects.filter(ticket=ticket, transition="merge").exists()


class TestTheMergeItemWaiveCommand(_ConfiguredOwner):
    def test_it_records_the_waiver(self) -> None:
        ticket = _ticket()
        verdict = _verdict(ticket, _FAIL, _PASS)

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

        assert unmet_items(_fresh(verdict)) == []

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
