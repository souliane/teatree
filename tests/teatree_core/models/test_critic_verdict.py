"""CriticVerdict (SELFCATCH-5): the async LLM critic's verdict, maker≠checker + never-fake-green.

``record`` refuses a maker/coding/loop ``grader_identity`` (self-attestation), mirroring
``ReviewVerdict.record``. ``CriticItemVerdict.coerce`` downgrades an uncited pass to
``instrumentation_gap`` (a FAIL) so a lazy model that waves an item through without
naming the artifact it inspected does not get a free pass.
"""

import pytest
from django.test import TestCase

from teatree.core.models import CriticItemVerdict, CriticVerdict, CriticVerdictError, Ticket
from teatree.core.models.critic_dispatch import CriticDispatch

_FORTY_HEX = "a" * 40
_JUDGED_ITEM = CriticItemVerdict("coherence", CriticItemVerdict.OK, "src/x.py:1")


class TestCriticItemVerdictCoerce(TestCase):
    def test_fail_stays_fail(self) -> None:
        item = CriticItemVerdict.coerce({"slug": "coherence", "status": "fail", "citation": "x.py:1"})
        assert item.status == CriticItemVerdict.FAIL
        assert item.is_fail()

    def test_pass_with_citation_stays_pass(self) -> None:
        item = CriticItemVerdict.coerce({"slug": "coherence", "status": "pass", "citation": "x.py:1"})
        assert item.status == CriticItemVerdict.OK
        assert not item.is_fail()

    def test_uncited_pass_downgrades_to_instrumentation_gap(self) -> None:
        item = CriticItemVerdict.coerce({"slug": "coherence", "status": "pass", "citation": ""})
        assert item.status == CriticItemVerdict.INSTRUMENTATION_GAP
        assert item.is_fail()  # counts as a fail

    def test_unknown_status_is_instrumentation_gap(self) -> None:
        item = CriticItemVerdict.coerce({"slug": "coherence", "status": "maybe", "citation": "x"})
        assert item.status == CriticItemVerdict.INSTRUMENTATION_GAP


class TestCriticVerdictRecord(TestCase):
    def test_records_an_independent_verdict(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        row = CriticVerdict.record(
            ticket=ticket,
            transition="mark_delivered",
            head_sha=_FORTY_HEX,
            grader_identity="critic-agent-7",
            items=[CriticItemVerdict("coherence", CriticItemVerdict.FAIL, "concept x conflated with y")],
        )
        assert row.grader_identity == "critic-agent-7"
        assert row.head_sha == _FORTY_HEX
        assert [i.slug for i in row.failed_items()] == ["coherence"]

    def test_refuses_a_maker_grader(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        with pytest.raises(CriticVerdictError) as exc:
            CriticVerdict.record(
                ticket=ticket,
                transition="mark_delivered",
                head_sha=_FORTY_HEX,
                grader_identity="merge-loop",
                items=[],
            )
        assert "maker" in str(exc.value)

    def test_refuses_an_anonymous_grader(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        with pytest.raises(CriticVerdictError):
            CriticVerdict.record(
                ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, grader_identity="  ", items=[]
            )

    def test_record_from_envelope_coerces_uncited_pass(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        row = CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="mark_delivered",
            head_sha=_FORTY_HEX,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [
                    {"slug": "coherence", "status": "pass", "citation": ""},  # uncited -> instrumentation_gap
                    {"slug": "duplication", "status": "pass", "citation": "searched render_ref"},
                ],
            },
        )
        failed = {i.slug for i in row.failed_items()}
        assert failed == {"coherence"}  # the uncited pass counts as a fail; the cited pass does not


class TestItemlessVerdictRefused(TestCase):
    """An envelope that judged nothing must not cover the head nor spend its dispatch."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)

    def test_missing_items_key_is_refused(self) -> None:
        with pytest.raises(CriticVerdictError, match="items is required"):
            CriticVerdict.record_from_envelope(
                ticket=self.ticket,
                transition="merge",
                head_sha=_FORTY_HEX,
                envelope={"grader_identity": "critic-agent-7"},
            )
        assert CriticVerdict.objects.latest_for(ticket=self.ticket, transition="merge") is None

    def test_refusal_leaves_the_dispatch_claim_re_armable(self) -> None:
        CriticDispatch.enqueue(ticket=self.ticket, transition="merge", head_sha=_FORTY_HEX, contract="judge it")

        with pytest.raises(CriticVerdictError):
            CriticVerdict.record_from_envelope(
                ticket=self.ticket,
                transition="merge",
                head_sha=_FORTY_HEX,
                envelope={"grader_identity": "critic-agent-7", "items": []},
            )

        dispatch = CriticDispatch.objects.get(ticket=self.ticket, transition="merge", head_sha=_FORTY_HEX)
        assert dispatch.state == CriticDispatch.State.DISPATCHED


class TestLatestFor(TestCase):
    def test_newest_wins_and_head_pin(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        older = CriticVerdict.record(
            ticket=ticket,
            transition="mark_delivered",
            head_sha=_FORTY_HEX,
            grader_identity="critic-1",
            items=[_JUDGED_ITEM],
        )
        newer = CriticVerdict.record(
            ticket=ticket,
            transition="mark_delivered",
            head_sha=_FORTY_HEX,
            grader_identity="critic-2",
            items=[_JUDGED_ITEM],
        )
        assert CriticVerdict.objects.latest_for(ticket=ticket, transition="mark_delivered").pk == newer.pk
        assert older.pk != newer.pk
        # A non-matching head pin finds nothing.
        assert CriticVerdict.objects.latest_for(ticket=ticket, transition="mark_delivered", head_sha="b" * 40) is None
