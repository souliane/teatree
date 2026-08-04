"""The SessionHandover drain — a backend-agnostic CAS that skips rows lost to a race (#3555).

Exactly one of N racing SessionStart hooks wins each row; a loser's conditional UPDATE
matches 0 rows and the drain moves on to the next claimable one.

Also the #4194 write seam: one unclaimed row per author, a target every row can
name someone for, and a merge that never drops what a previous hand-off carried.
"""

from unittest import mock

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import SessionHandover
from teatree.core.session_handover_manager import SelfAddressedHandoverError, SessionHandoverQuerySet


class TestClaimAll(TestCase):
    def test_drains_every_claimable_row_targeted_first(self) -> None:
        targeted = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="p1")
        broadcast = SessionHandover.objects.create_handover(from_session="c", to_session="", payload="p2")

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


class TestOneUnclaimedRowPerSession(TestCase):
    """A second ``create`` from one author updates its row instead of adding a sibling (#4194)."""

    def test_a_second_create_reuses_the_same_row(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST")
        second = SessionHandover.objects.create_handover(from_session="a", to_session="c", payload="SECOND")

        assert second.pk == first.pk
        assert SessionHandover.objects.filter(from_session="a", claimed_at__isnull=True).count() == 1
        assert second.to_session == "c"

    def test_the_previous_payload_is_kept_not_destroyed(self) -> None:
        """22,224 bytes of state were at stake in the measured incident — nothing is dropped."""
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST STATE")
        merged = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SECOND STATE")

        assert "FIRST STATE" in merged.payload
        assert "SECOND STATE" in merged.payload
        assert merged.payload.index("FIRST STATE") < merged.payload.index("SECOND STATE")
        assert "from `a`" in merged.payload, "the absorbed segment is fenced with its author"

    def test_an_identical_repeat_is_not_duplicated(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SAME")
        merged = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SAME")
        assert merged.payload.count("SAME") == 1

    def test_created_at_is_refreshed_to_the_latest_write(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST")
        was = first.created_at
        second = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SECOND")
        assert second.created_at > was

    def test_a_claimed_row_is_never_reused(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="DELIVERED")
        SessionHandover.objects.claim_all("b")
        second = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="NEW WORK")

        assert second.pk != first.pk
        assert SessionHandover.objects.count() == 2

    def test_a_raw_second_unclaimed_row_is_refused_by_the_database(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST")
        with pytest.raises(IntegrityError), transaction.atomic():
            SessionHandover.objects.create(from_session="a", to_session="b", payload="SIBLING")

    def test_a_racing_insert_lands_on_the_update_branch(self) -> None:
        """Two concurrent creates both see no existing row; the loser must merge, not raise."""
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="WINNER")
        original = SessionHandoverQuerySet._unclaimed_for
        seen: list[int] = []

        def _blind_once(self: SessionHandoverQuerySet, from_session: str) -> SessionHandover | None:
            seen.append(1)
            return None if len(seen) == 1 else original(self, from_session)

        with mock.patch.object(SessionHandoverQuerySet, "_unclaimed_for", _blind_once):
            merged = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="LOSER")

        assert SessionHandover.objects.filter(from_session="a", claimed_at__isnull=True).count() == 1
        assert "WINNER" in merged.payload
        assert "LOSER" in merged.payload


class TestATargetAlwaysNamesSomebodyWhoCanClaim(TestCase):
    """No row the DB accepts may be claimable by nobody (#4194)."""

    def test_the_loop_runner_principal_is_parked_at_the_write_seam(self) -> None:
        row = SessionHandover.objects.create_handover(from_session="a", to_session="loop-runner", payload="P")
        assert row.to_session == ""
        assert row in SessionHandover.objects.claimable_for("any-starting-session")

    def test_a_raw_loop_runner_target_is_refused_by_the_database(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            SessionHandover.objects.create(from_session="a", to_session="loop-runner", payload="P")

    def test_a_raw_self_addressed_row_is_refused_by_the_database(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            SessionHandover.objects.create(from_session="s1", to_session="s1", payload="P")

    def test_an_anonymous_parked_row_is_still_accepted(self) -> None:
        """``from_session == to_session == ""`` IS claimable by anyone — the check must not refuse it."""
        row = SessionHandover.objects.create(from_session="", to_session="", payload="P")
        assert row in SessionHandover.objects.claimable_for("whoever")

    def test_the_manager_still_refuses_a_self_addressed_target(self) -> None:
        with pytest.raises(SelfAddressedHandoverError):
            SessionHandover.objects.create_handover(from_session="s1", to_session="s1", payload="P")

    def test_every_accepted_row_shape_has_a_session_that_can_claim_it(self) -> None:
        shapes = [
            ("author-1", "receiver-1"),
            ("author-2", ""),
            ("", ""),
        ]
        for from_session, to_session in shapes:
            row = SessionHandover.objects.create_handover(from_session=from_session, to_session=to_session, payload="P")
            candidate = to_session or "some-other-session"
            assert row in SessionHandover.objects.claimable_for(candidate), (
                f"a row {from_session!r} -> {to_session!r} the DB accepts must be claimable by somebody"
            )
