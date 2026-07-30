"""The SessionHandover drain — a backend-agnostic CAS that skips rows lost to a race (#3555).

Exactly one of N racing SessionStart hooks wins each row; a loser's conditional UPDATE
matches 0 rows and the drain moves on to the next claimable one.
"""

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import SessionHandover
from teatree.core.session_handover_manager import SessionHandoverQuerySet


class TestClaimAll(TestCase):
    def test_drains_every_claimable_row_targeted_first(self) -> None:
        targeted = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="p1")
        broadcast = SessionHandover.objects.create_handover(from_session="a", to_session="", payload="p2")

        claimed = SessionHandover.objects.claim_all("b")

        assert {row.pk for row in claimed} == {targeted.pk, broadcast.pk}
        # The explicitly-addressed hand-off is delivered before the open broadcast.
        assert claimed[0].pk == targeted.pk

    def test_a_session_never_claims_its_own_handover(self) -> None:
        SessionHandover.objects.create_handover(from_session="b", to_session="", payload="mine")
        assert SessionHandover.objects.claim_all("b") == []

    def test_a_row_lost_to_a_concurrent_claim_is_skipped(self) -> None:
        # A row that was claimable at snapshot time but got claimed by a concurrent
        # SessionStart hook before this drain's CAS reaches it must be skipped (its
        # conditional UPDATE matches 0 rows), never delivered twice. Widening the
        # candidate set to include an already-claimed row reproduces that race.
        already = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="taken")
        SessionHandover.objects.filter(pk=already.pk).update(claimed_at=timezone.now(), claimed_by="rival")
        fresh = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="fresh")

        def _all_rows(self: SessionHandoverQuerySet, _session_id: str) -> SessionHandoverQuerySet:
            return self.all()

        with mock.patch.object(SessionHandoverQuerySet, "claimable_for", _all_rows):
            claimed = SessionHandover.objects.claim_all("b")

        assert [row.pk for row in claimed] == [fresh.pk]
        already.refresh_from_db()
        assert already.claimed_by == "rival"
