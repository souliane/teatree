import pytest
from django.test import TestCase

from teatree.core.models import Rubric, RubricError, Ticket
from teatree.core.models.rubric_waiver import waive_criterion
from teatree.core.models.types import validated_ticket_extra

_SHA = "a" * 40


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)


class TestWaiveCriterion(TestCase):
    def _rubric(self) -> tuple[Ticket, Rubric, str]:
        ticket = _ticket()
        rubric = Rubric.populate(ticket, ["AC1", "AC2"])
        return ticket, rubric, rubric.criteria.get(ordinal=0).text_hash

    def test_waiver_records_identity_reason_time_and_removes_only_the_row(self) -> None:
        ticket, rubric, text_hash = self._rubric()
        waive_criterion(ticket, text_hash=text_hash, waived_by="owner", reason="out of scope")
        ticket.refresh_from_db()
        assert list(rubric.criteria.values_list("text", flat=True)) == ["AC2"]
        waiver = ticket.extra["rubric_waivers"][0]
        assert waiver["text_hash"] == text_hash
        assert waiver["text"] == "AC1"
        assert waiver["waived_by"] == "owner"
        assert waiver["reason"] == "out of scope"
        assert waiver["waived_at"]
        assert validated_ticket_extra(ticket.extra)["rubric_waivers"] == [waiver]

    def _assert_refused(self, *, waived_by: str, reason: str, hash_override: str | None, match: str) -> None:
        ticket, rubric, text_hash = self._rubric()
        with pytest.raises(RubricError, match=match):
            waive_criterion(ticket, text_hash=hash_override or text_hash, waived_by=waived_by, reason=reason)
        ticket.refresh_from_db()
        assert rubric.criteria.count() == 2
        assert not ticket.extra.get("rubric_waivers")

    def test_agent_identity_is_refused(self) -> None:
        self._assert_refused(waived_by="coding-agent", reason="out of scope", hash_override=None, match="agent")

    def test_empty_reason_is_refused(self) -> None:
        self._assert_refused(waived_by="owner", reason=" ", hash_override=None, match="reason")

    def test_unknown_hash_is_refused(self) -> None:
        self._assert_refused(waived_by="owner", reason="out of scope", hash_override="0" * 12, match="unknown")

    def test_last_criterion_is_refused(self) -> None:
        ticket = _ticket()
        rubric = Rubric.populate(ticket, ["AC1"])
        with pytest.raises(RubricError, match="last"):
            waive_criterion(ticket, text_hash=rubric.criteria.get().text_hash, waived_by="owner", reason="out of scope")
        ticket.refresh_from_db()
        assert rubric.criteria.count() == 1
        assert not ticket.extra.get("rubric_waivers")

    def test_failed_criterion_is_refused(self) -> None:
        ticket, rubric, text_hash = self._rubric()
        rubric.criteria.get(ordinal=0).record_grade(status="fail", grader_identity="cold-reviewer", reviewed_sha=_SHA)
        with pytest.raises(RubricError, match="FAIL"):
            waive_criterion(ticket, text_hash=text_hash, waived_by="owner", reason="out of scope")
        ticket.refresh_from_db()
        assert rubric.criteria.count() == 2
        assert not ticket.extra.get("rubric_waivers")

    def test_later_plan_addition_skips_waived_text_but_explicit_populate_can_restore_it(self) -> None:
        ticket, rubric, text_hash = self._rubric()
        waive_criterion(ticket, text_hash=text_hash, waived_by="owner", reason="out of scope")
        Rubric.add_criteria(ticket, ["AC1", "AC3"])
        assert list(rubric.criteria.values_list("text", flat=True)) == ["AC2", "AC3"]
        Rubric.populate(ticket, ["AC1", "AC2"])
        assert list(rubric.criteria.values_list("text", flat=True)) == ["AC1", "AC2"]
