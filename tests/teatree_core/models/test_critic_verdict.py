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
from teatree.core.models.critic_verdict import CriticEvidenceUnavailableError

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

    def test_explicitly_unverified_failure_is_not_a_failed_item(self) -> None:
        item = CriticItemVerdict.coerce(
            {"slug": "cleanliness", "status": "fail", "citation": "diff unavailable", "verified": False}
        )
        assert item.status == CriticItemVerdict.UNVERIFIED
        assert not item.is_fail()

    def test_nit_only_failure_passes_the_item_and_preserves_its_point(self) -> None:
        item = CriticItemVerdict.coerce(
            {
                "slug": "cleanliness",
                "status": "fail",
                "citation": "x.py:2",
                "points": [{"kind": "nit", "summary": "rename helper", "citation": "x.py:2"}],
            }
        )

        assert item.status == CriticItemVerdict.OK
        assert not item.is_fail()
        assert item.as_dict()["points"][0]["kind"] == "nit"
        assert item.as_dict()["legacy_status"] == "fail"

    def test_blocking_point_overrides_a_pass(self) -> None:
        item = CriticItemVerdict.coerce(
            {
                "slug": "test_value",
                "status": "pass",
                "citation": "tests/x.py:4",
                "points": [{"kind": "blocking", "summary": "vacuous test", "citation": "tests/x.py:4"}],
            }
        )

        assert item.status == CriticItemVerdict.FAIL
        assert item.is_fail()

    def test_critic_nit_label_cannot_override_major_severity(self) -> None:
        item = CriticItemVerdict.coerce(
            {
                "slug": "cleanliness",
                "status": "pass",
                "citation": "x.py:2",
                "points": [{"kind": "nit", "severity": "major", "summary": "broken behavior"}],
            }
        )
        assert item.status == CriticItemVerdict.FAIL


class TestCriticVerdictRecord(TestCase):
    def test_new_head_records_every_prior_finding_resolution(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        previous = CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha="b" * 40,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [
                    {
                        "slug": "cleanliness",
                        "status": "fail",
                        "citation": "src/x.py:4",
                        "points": [
                            {"kind": "blocking", "summary": "duplicate function", "citation": "src/x.py:4"},
                        ],
                    }
                ],
            },
        )
        point_id = previous.item_verdicts()[0].points[0].id
        assert point_id == f"{'b' * 40}:cleanliness:1"
        with pytest.raises(CriticVerdictError, match="previous findings"):
            CriticVerdict.record_from_envelope(
                ticket=ticket,
                transition="merge",
                head_sha=_FORTY_HEX,
                envelope={
                    "grader_identity": "critic-agent-8",
                    "items": [
                        {"slug": "cleanliness", "status": "pass", "citation": "src/x.py:4"},
                    ],
                },
            )
        assert CriticVerdict.objects.latest_for(ticket=ticket, transition="merge", head_sha=_FORTY_HEX) is None

        with pytest.raises(CriticVerdictError, match="previous findings"):
            CriticVerdict.record_from_envelope(
                ticket=ticket,
                transition="merge",
                head_sha=_FORTY_HEX,
                envelope={
                    "grader_identity": "critic-agent-8",
                    "items": [
                        {
                            "slug": "cleanliness",
                            "status": "pass",
                            "citation": "src/x.py:4",
                            "resolutions": [
                                {"finding_id": "duplicate function", "status": "fixed", "citation": "src/x.py:4"}
                            ],
                        }
                    ],
                },
            )

        verdict = CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha=_FORTY_HEX,
            envelope={
                "grader_identity": "critic-agent-8",
                "items": [
                    {
                        "slug": "cleanliness",
                        "status": "pass",
                        "citation": "src/x.py:4",
                        "resolutions": [
                            {"finding_id": point_id, "status": "fixed", "citation": "src/x.py:4"},
                        ],
                    },
                ],
            },
        )
        assert verdict.item_verdicts()[0].as_dict()["resolutions"] == [
            {"finding_id": point_id, "status": "fixed", "citation": "src/x.py:4"}
        ]

    def test_prior_failed_item_without_points_also_requires_resolution(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha="b" * 40,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [
                    {"slug": "test_value", "status": "fail", "citation": "tests/x.py:3"},
                ],
            },
        )
        with pytest.raises(CriticVerdictError, match="previous findings"):
            CriticVerdict.record_from_envelope(
                ticket=ticket,
                transition="merge",
                head_sha=_FORTY_HEX,
                envelope={
                    "grader_identity": "critic-agent-8",
                    "items": [
                        {"slug": "test_value", "status": "pass", "citation": "tests/x.py:3"},
                    ],
                },
            )

    def test_unfixed_blocking_point_cannot_be_reported_as_a_pass(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        prior = CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha="b" * 40,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [
                    {
                        "slug": "cleanliness",
                        "status": "fail",
                        "citation": "src/x.py:4",
                        "points": [{"kind": "blocking", "summary": "duplicate", "citation": "src/x.py:4"}],
                    }
                ],
            },
        )
        point_id = prior.item_verdicts()[0].points[0].id
        for status, citation in (("not_fixed", "src/x.py:4"), ("fixed", ""), ("ignored", "src/x.py:4")):
            with self.subTest(status=status, citation=citation), pytest.raises(CriticEvidenceUnavailableError):
                CriticVerdict.record_from_envelope(
                    ticket=ticket,
                    transition="merge",
                    head_sha=_FORTY_HEX,
                    envelope={
                        "grader_identity": "critic-agent-8",
                        "items": [
                            {
                                "slug": "cleanliness",
                                "status": "pass",
                                "citation": "src/x.py:4",
                                "resolutions": [{"finding_id": point_id, "status": status, "citation": citation}],
                            }
                        ],
                    },
                )

    def test_a_nit_follow_up_cannot_defer_its_critic_nits_again(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree", state=Ticket.State.DELIVERED, extra={"nit_follow_up": True}
        )

        row = CriticVerdict.record_from_envelope(
            ticket=ticket,
            transition="merge",
            head_sha=_FORTY_HEX,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [
                    {
                        "slug": "cleanliness",
                        "status": "fail",
                        "citation": "x.py:2",
                        "points": [{"kind": "nit", "summary": "rename helper", "citation": "x.py:2"}],
                    }
                ],
            },
        )

        assert [item.slug for item in row.failed_items()] == ["cleanliness"]
        assert row.item_verdicts()[0].points[0].kind == "blocking"

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

    def test_unverified_item_refuses_the_whole_verdict(self) -> None:
        with pytest.raises(CriticEvidenceUnavailableError, match="not graded"):
            CriticVerdict.record_from_envelope(
                ticket=self.ticket,
                transition="merge",
                head_sha=_FORTY_HEX,
                envelope={
                    "grader_identity": "critic-agent-7",
                    "items": [
                        {"slug": "test_value", "status": "pass", "citation": "tests/x.py:1"},
                        {"slug": "cleanliness", "status": "fail", "citation": "diff unavailable", "verified": False},
                    ],
                },
            )
        assert CriticVerdict.objects.latest_for(ticket=self.ticket, transition="merge") is None


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
