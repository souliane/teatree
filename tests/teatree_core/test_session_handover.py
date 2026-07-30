"""Tests for the session-to-session hand-off model + manager.

Covers the drain CAS (to the loop owner, to an explicit id, parked for next
session), the no-self-claim invariant, and the explicit-target preference. The
claim mirrors the ``LoopLease`` backend-agnostic conditional-UPDATE shape
(SQLite-prod, no ``select_for_update``), so the keystone is that a second
claimant of an already-claimed row wins nothing.
"""

from django.test import TestCase

from teatree.core.models import SessionHandover


class TestSessionHandoverCreate(TestCase):
    def test_create_to_explicit_session(self) -> None:
        h = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="P")
        assert h.from_session == "a"
        assert h.to_session == "b"
        assert h.is_for_next_session is False
        assert h.claimed_at is None

    def test_create_for_next_session_has_empty_target(self) -> None:
        h = SessionHandover.objects.create_handover(from_session="a", to_session="", payload="P")
        assert h.is_for_next_session is True


class TestSessionHandoverClaim(TestCase):
    def test_explicit_target_session_can_claim(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="P")
        claimed = SessionHandover.objects.claim_all("b")
        assert [row.payload for row in claimed] == ["P"]
        assert claimed[0].claimed_by == "b"
        assert claimed[0].claimed_at is not None

    def test_next_session_handover_claimable_by_any_other_session(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="", payload="P")
        assert [row.payload for row in SessionHandover.objects.claim_all("whoever-starts-next")] == ["P"]

    def test_session_never_claims_its_own_handover(self) -> None:
        # A same-session compact resume must not re-inject its own snapshot.
        SessionHandover.objects.create_handover(from_session="a", to_session="", payload="P")
        assert SessionHandover.objects.claim_all("a") == []

    def test_explicit_target_excludes_other_named_sessions(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="P")
        assert SessionHandover.objects.claim_all("c") == []

    def test_claim_is_single_use(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="P")
        assert SessionHandover.objects.claim_all("b") != []
        assert SessionHandover.objects.claim_all("b") == []

    def test_already_claimed_row_yields_nothing_to_second_claimant(self) -> None:
        # The CAS keystone: claiming an already-claimed row matches 0 rows.
        h = SessionHandover.objects.create_handover(from_session="a", to_session="", payload="P")
        first = SessionHandover.objects.claim_all("b")
        assert [row.pk for row in first] == [h.pk]
        assert SessionHandover.objects.claim_all("c") == []

    def test_explicit_target_preferred_over_next_session(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="", payload="NEXT")
        SessionHandover.objects.create_handover(from_session="x", to_session="b", payload="MINE")
        assert [row.payload for row in SessionHandover.objects.claim_all("b")] == ["MINE", "NEXT"]

    def test_nothing_claimable_returns_empty(self) -> None:
        assert SessionHandover.objects.claim_all("b") == []
