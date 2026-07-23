"""Parked hand-offs are DRAINED, not sampled (#3555), and never empty (#3551/#3563).

Three defects, one delivery path:

- ``claim_next`` returned on the first CAS win, newest-first, so every older
    parked row starved forever — the next session to start again found a newer row
    on top. The queue is now drained oldest-first within the parked tier.
- ``handover create`` reported ``OK`` over a stub payload that told the receiver
    to re-derive everything. It now derives a payload from live DB state and
    refuses loudly when even that is empty.
- ``latest.md`` pointed at whichever mirror was written last rather than the
    newest hand-off.
"""

import datetime as dt
import tempfile
from pathlib import Path

from django.test import TestCase
from django.utils import timezone

from teatree.core.handover import HandoverPayload, claim_handovers, render_claimed_payload, write_mirror
from teatree.core.models import SessionHandover, Ticket, Worktree

_PARKED_COUNT = 8


def _park(from_session: str, *, minutes_ago: int, payload: str) -> SessionHandover:
    row = SessionHandover.objects.create_handover(from_session=from_session, to_session="", payload=payload)
    SessionHandover.objects.filter(pk=row.pk).update(created_at=timezone.now() - dt.timedelta(minutes=minutes_ago))
    row.refresh_from_db()
    return row


class TestParkedHandoversAreDrained(TestCase):
    def test_all_eight_parked_rows_are_delivered(self) -> None:
        for index in range(_PARKED_COUNT):
            _park(f"session-{index}", minutes_ago=_PARKED_COUNT - index, payload=f"payload-{index}")

        payload, origin = claim_handovers("starting-session")

        assert SessionHandover.objects.filter(claimed_at__isnull=True).count() == 0, (
            "every parked hand-off must be delivered — none may starve"
        )
        for index in range(_PARKED_COUNT):
            assert f"payload-{index}" in payload
        assert origin == f"{_PARKED_COUNT} sessions"

    def test_the_parked_tier_is_delivered_oldest_first(self) -> None:
        _park("newer", minutes_ago=1, payload="NEWER")
        _park("older", minutes_ago=30, payload="OLDER")

        payload, _origin = claim_handovers("starting-session")

        assert payload.index("OLDER") < payload.index("NEWER")

    def test_a_targeted_handover_leads(self) -> None:
        _park("parked-author", minutes_ago=30, payload="PARKED")
        SessionHandover.objects.create_handover(from_session="direct", to_session="me", payload="TARGETED")

        payload, _origin = claim_handovers("me")

        assert payload.index("TARGETED") < payload.index("PARKED")

    def test_a_session_never_claims_its_own_handover(self) -> None:
        _park("me", minutes_ago=5, payload="MINE")

        payload, origin = claim_handovers("me")

        assert payload == ""
        assert origin == ""

    def test_a_lone_handover_is_delivered_verbatim(self) -> None:
        _park("author", minutes_ago=5, payload="ONLY")

        payload, origin = claim_handovers("starting-session")

        assert payload == "ONLY"
        assert origin == "author"


class TestRenderClaimedPayload(TestCase):
    """Concatenating a drained batch fences each author's state (#3555)."""

    def test_a_lone_handover_renders_as_its_bare_payload(self) -> None:
        row = _park("solo", minutes_ago=5, payload="JUST ME")
        assert render_claimed_payload([row]) == "JUST ME"

    def test_multiple_handovers_are_fenced_by_authored_headers(self) -> None:
        first = _park("author-a", minutes_ago=30, payload="STATE A")
        second = _park("author-b", minutes_ago=5, payload="STATE B")

        rendered = render_claimed_payload([first, second])

        assert "STATE A" in rendered
        assert "STATE B" in rendered
        # Each is fenced with a header naming its author so two authors' state is
        # never read as one narrative.
        assert "from `author-a`" in rendered
        assert "from `author-b`" in rendered
        assert "Hand-off 1 of 2" in rendered
        assert "Hand-off 2 of 2" in rendered


class TestLiveStatePayloadFallback(TestCase):
    def test_in_flight_work_is_derived_when_no_snapshot_exists(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.invalid/org/repo/issues/7")
        Worktree.objects.create(
            ticket=ticket,
            overlay="",
            repo_path="org/repo",
            branch="7-fix-the-thing",
            state=Worktree.State.READY,
            extra={"worktree_path": "/tmp/wt/7"},
        )

        payload = HandoverPayload("sess").live_state()

        assert "7-fix-the-thing" in payload
        assert str(ticket.pk) in payload

    def test_an_idle_box_derives_nothing(self) -> None:
        assert HandoverPayload("sess").live_state() == ""


class TestLatestPointerTracksTheNewestHandover(TestCase):
    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = Path(tmp_dir.name)

    def test_pointer_follows_the_newest_even_when_an_older_row_is_mirrored_last(self) -> None:
        pointer = self.tmp_path / "latest.md"
        newest = _park("newest-author", minutes_ago=1, payload="NEWEST")
        oldest = _park("oldest-author", minutes_ago=60, payload="OLDEST")

        write_mirror(newest, pointer)
        write_mirror(oldest, pointer)

        assert "NEWEST" in pointer.read_text(encoding="utf-8")
