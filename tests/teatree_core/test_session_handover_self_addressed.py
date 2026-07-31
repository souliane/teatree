"""A self-addressed hand-off is unclaimable by every session, forever (#3821).

``claimable_for`` requires ``to_session == session_id or to_session == ""`` AND
excludes ``from_session == session_id``. When a row's ``from_session`` equals its
``to_session`` those two are mutually exclusive: the only session the address
admits is the one the exclusion removes. The row is not waiting on anyone — it is
structurally unreachable, while still counting as pending in the queue.

The exclusion is correct and stays (it is what stops a same-session compact resume
re-injecting its own snapshot), so the degenerate row is refused where an operator
can still react to it: at creation. Rows already persisted are parked for the next
session by the ``0048`` migration, since an unreachable row holding real state is
work-bearing state made terminal.
"""

import pytest
from django.test import TestCase

from teatree.core.handover import create_handover
from teatree.core.models import SessionHandover
from teatree.core.session_handover_manager import SelfAddressedHandoverError


class TestSelfAddressedRowIsUnreachable(TestCase):
    """The property that motivates the refusal — asserted directly, on a row built by hand."""

    def test_no_session_id_can_claim_it(self) -> None:
        row = SessionHandover.objects.create(from_session="s1", to_session="s1", payload="real state")
        for candidate in ("s1", "s2", ""):
            assert row not in SessionHandover.objects.claimable_for(candidate), (
                f"session {candidate!r} must not be able to claim it — that is the defect"
            )


class TestCreationRefusesTheDegenerateHandover(TestCase):
    def test_manager_refuses_a_self_addressed_row(self) -> None:
        with pytest.raises(SelfAddressedHandoverError):
            SessionHandover.objects.create_handover(from_session="s1", to_session="s1", payload="state")
        assert SessionHandover.objects.count() == 0, "the unreachable row must not be persisted"

    def test_service_refuses_an_explicit_self_target(self) -> None:
        with pytest.raises(SelfAddressedHandoverError):
            create_handover(from_session="s1", explicit_to="s1", authored="state")
        assert SessionHandover.objects.count() == 0

    def test_a_genuine_target_still_creates(self) -> None:
        row = SessionHandover.objects.create_handover(from_session="s1", to_session="s2", payload="state")
        assert row.pk is not None
        assert row in SessionHandover.objects.claimable_for("s2")

    def test_a_parked_handover_still_creates(self) -> None:
        row = SessionHandover.objects.create_handover(from_session="s1", to_session="", payload="state")
        assert row in SessionHandover.objects.claimable_for("s2")
