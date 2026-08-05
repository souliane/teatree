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
from teatree.core.session_handover_manager import (
    SelfAddressedHandoverError,
    SessionHandoverQuerySet,
    render_fenced_handoffs,
)


def _blind_first_lookup() -> "mock._patch":
    """Blind the first ``_unclaimed_for`` call, so the insert races and the retry absorbs."""
    original = SessionHandoverQuerySet._unclaimed_for
    seen: list[int] = []

    def _blind_once(self: SessionHandoverQuerySet, from_session: str) -> "SessionHandover | None":
        seen.append(1)
        return None if len(seen) == 1 else original(self, from_session)

    return mock.patch.object(SessionHandoverQuerySet, "_unclaimed_for", _blind_once)


def _resolving_to(stale: "SessionHandover") -> "mock._patch":
    """Resolve the absorb target to *stale*, an instance read before a rival's write landed."""
    return mock.patch.object(SessionHandoverQuerySet, "_unclaimed_for", return_value=stale)


class TestClaimAll(TestCase):
    def test_drains_every_claimable_row_targeted_first(self) -> None:
        targeted = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="p1").row
        broadcast = SessionHandover.objects.create_handover(from_session="c", to_session="", payload="p2").row

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
        already = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="taken").row
        SessionHandover.objects.filter(pk=already.pk).update(claimed_at=timezone.now(), claimed_by="rival")
        fresh = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="fresh").row

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
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST").row
        second = SessionHandover.objects.create_handover(from_session="a", to_session="c", payload="SECOND").row

        assert second.pk == first.pk
        assert SessionHandover.objects.filter(from_session="a", claimed_at__isnull=True).count() == 1
        assert second.to_session == "c"

    def test_the_previous_payload_is_kept_not_destroyed(self) -> None:
        """22,224 bytes of state were at stake in the measured incident — nothing is dropped."""
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST STATE")
        merged = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SECOND STATE").row

        assert "FIRST STATE" in merged.payload
        assert "SECOND STATE" in merged.payload
        assert merged.payload.index("FIRST STATE") < merged.payload.index("SECOND STATE")
        assert "from `a`" in merged.payload, "the absorbed segment is fenced with its author"

    def test_an_identical_repeat_is_not_duplicated(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SAME")
        merged = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SAME").row
        assert merged.payload.count("SAME") == 1

    def test_a_duplicate_drop_is_reported_apart_from_an_append(self) -> None:
        """A dropped duplicate and an append are otherwise indistinguishable in the report.

        Both land ``ok`` on the same row; the byte counts match too, since the drop adds
        none. The receiver gets the bytes either way — they were already there — but the
        operator has no way to tell a hand-off that added state from one that added none.
        """
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SAME")
        repeat = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SAME")
        added = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="NEW")

        assert first.payload_appended is True
        assert repeat.payload_appended is False
        assert repeat.previous_bytes == len(repeat.row.payload), "the drop is invisible in the byte counts alone"
        assert added.payload_appended is True

    def test_created_at_is_refreshed_to_the_latest_write(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST").row
        was = first.created_at
        second = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SECOND").row
        assert second.created_at > was

    def test_a_claimed_row_is_never_reused(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="DELIVERED").row
        SessionHandover.objects.claim_all("b")
        second = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="NEW WORK").row

        assert second.pk != first.pk
        assert SessionHandover.objects.count() == 2

    def test_a_raw_second_unclaimed_row_is_refused_by_the_database(self) -> None:
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST")
        with pytest.raises(IntegrityError), transaction.atomic():
            SessionHandover.objects.create(from_session="a", to_session="b", payload="SIBLING")

    def test_a_racing_insert_lands_on_the_update_branch(self) -> None:
        """Two concurrent creates both see no existing row; the loser must merge, not raise."""
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="WINNER")

        with _blind_first_lookup():
            merged = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="LOSER").row

        assert SessionHandover.objects.filter(from_session="a", claimed_at__isnull=True).count() == 1
        assert "WINNER" in merged.payload
        assert "LOSER" in merged.payload

    def test_the_manager_reports_the_absorbed_bytes_on_the_integrity_retry(self) -> None:
        """The absorb is reported from the write seam, so the retry path cannot under-report it.

        A pre-read in the caller saw no row on the retry path (the lookup it copied was
        the one the race blinded), so an absorb was announced as a fresh insert:
        ``updated_existing: false, previous_payload_bytes: 0`` — silent in exactly the
        way those two fields exist to prevent.
        """
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="WINNER").row

        with _blind_first_lookup():
            write = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="LOSER")

        assert write.absorbed is True
        assert write.previous_bytes == len(first.payload)
        assert write.row.pk == first.pk

    def test_a_first_write_reports_no_absorb(self) -> None:
        write = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST")
        assert write.absorbed is False
        assert write.previous_bytes == 0

    def test_an_ordinary_absorb_reports_the_bytes_it_landed_on(self) -> None:
        first = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="FIRST").row
        write = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="SECOND")
        assert write.absorbed is True
        assert write.previous_bytes == len(first.payload)

    def test_an_absorb_onto_a_row_a_rival_absorb_already_extended_keeps_both(self) -> None:
        """Two absorbs for one author that resolved the same prior row must both survive.

        The absorb reads the payload, appends to it and writes the result back. Two
        calls that resolved the same row read the same prior bytes, so the later write
        carries no trace of the earlier one and drops it — the lost update the absorb
        exists to prevent, at the seam that implements it. The prior payload has to come
        from a read taken UNDER the write lock, never from the instance the caller
        resolved before the rival landed.
        """
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="PRIOR")
        stale = SessionHandover.objects.get(from_session="a")
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="RIVAL")

        with _resolving_to(stale):
            SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="LATER")

        payload = SessionHandover.objects.get(from_session="a").payload
        assert "PRIOR" in payload
        assert "RIVAL" in payload, "the rival absorb's bytes were overwritten by a stale read-modify-write"
        assert "LATER" in payload

    def test_the_absorbed_bytes_are_reported_from_the_locked_read_not_the_stale_one(self) -> None:
        """``previous_payload_bytes`` names what the write landed ON, so it counts the rival too."""
        SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="PRIOR")
        stale = SessionHandover.objects.get(from_session="a")
        rival = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="RIVAL").row

        with _resolving_to(stale):
            write = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="LATER")

        assert write.previous_bytes == len(rival.payload)


class TestATargetAlwaysNamesSomebodyWhoCanClaim(TestCase):
    """No row the DB accepts may be claimable by nobody (#4194)."""

    def test_the_loop_runner_principal_is_parked_at_the_write_seam(self) -> None:
        row = SessionHandover.objects.create_handover(from_session="a", to_session="loop-runner", payload="P").row
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
            write = SessionHandover.objects.create_handover(
                from_session=from_session, to_session=to_session, payload="P"
            )
            row = write.row
            candidate = to_session or "some-other-session"
            assert row in SessionHandover.objects.claimable_for(candidate), (
                f"a row {from_session!r} -> {to_session!r} the DB accepts must be claimable by somebody"
            )


class TestRenderFencedHandoffs:
    """The one fence shape both the drain and the duplicate-collapse migration emit."""

    def test_a_lone_entry_renders_as_its_bare_payload(self) -> None:
        assert render_fenced_handoffs([("solo", "2026-08-04T10:00:00", "JUST ME")]) == "JUST ME"

    def test_several_entries_are_each_fenced_with_their_author_and_instant(self) -> None:
        rendered = render_fenced_handoffs(
            [("author-a", "2026-08-04T10:00:00", "STATE A"), ("author-b", "2026-08-04T10:30:00", "STATE B")]
        )
        assert rendered.index("STATE A") < rendered.index("STATE B")
        assert "Hand-off 1 of 2 — from `author-a` at 2026-08-04T10:00:00" in rendered
        assert "Hand-off 2 of 2 — from `author-b` at 2026-08-04T10:30:00" in rendered

    def test_no_entries_render_nothing(self) -> None:
        assert render_fenced_handoffs([]) == ""


class TestAbsorbOntoAnEmptyRow(TestCase):
    def test_an_incoming_payload_lands_bare_on_a_row_that_holds_nothing(self) -> None:
        """A raw empty row is the only way in now that an EMPTY create writes no row at all.

        There is nothing to fence the incoming payload BEHIND, so fencing it would
        open the receiver's hand-off with an empty ``Hand-off update`` header.
        """
        SessionHandover.objects.create(from_session="a", to_session="b", payload="")

        merged = SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="THE STATE").row

        assert merged.payload == "THE STATE"

    def test_an_integrity_error_the_retry_cannot_explain_is_re_raised(self) -> None:
        """Only the duplicate-author race is absorbed; any other constraint failure stays loud."""

        def _always_blind(_self: SessionHandoverQuerySet, _from_session: str) -> None:
            return None

        with (
            mock.patch.object(SessionHandoverQuerySet, "_unclaimed_for", _always_blind),
            mock.patch.object(SessionHandoverQuerySet, "create", side_effect=IntegrityError("some other constraint")),
            pytest.raises(IntegrityError),
        ):
            SessionHandover.objects.create_handover(from_session="a", to_session="b", payload="P")
