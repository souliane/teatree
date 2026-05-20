"""Tests for the multi-loop anchors-zone rendering (#1156).

The pre-#1156 statusline rendered a verbose ``loop-owner=THIS session ✓``
or ``loop-owner=unclaimed`` line plus the foreign-hijack RED line.
With five named loops now in flight (loop-owner, loop-tick,
loop-self-improve, loop-slack-answer, loop-slot) the dim doctrine
collapses into one line per LIVE LoopLease row, and the only RED
loop line that survives is the #1073 foreign-hijack one.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from teatree.core.models.loop_lease import LoopLease
from teatree.loop.statusline import StatuslineZones, live_loops_anchor


def _make_lease(name: str, *, expires_in: timedelta, session_id: str = "sess-A") -> LoopLease:
    return LoopLease.objects.create(
        name=name,
        owner=session_id or "sess-A",
        session_id=session_id,
        acquired_at=timezone.now(),
        lease_expires_at=timezone.now() + expires_in,
    )


@pytest.mark.django_db
class TestLiveLoopsAnchor:
    """``live_loops_anchor()`` returns one line per live LoopLease (#1156)."""

    def test_anchors_show_one_line_per_live_loop(self) -> None:
        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))
        _make_lease("loop-slack-answer", expires_in=timedelta(minutes=30))

        lines = live_loops_anchor()

        assert len(lines) == 3, repr(lines)
        # Each line should start with the short loop name (loop- prefix stripped).
        names = sorted(line.split()[0] for line in lines)
        assert names == ["loop:self-improve", "loop:slack-answer", "loop:tick"]

    def test_expired_loops_omitted(self) -> None:
        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))
        # Expired — must be filtered out.
        _make_lease("loop-slack-answer", expires_in=timedelta(seconds=-5))

        lines = live_loops_anchor()

        assert len(lines) == 2, repr(lines)
        names = sorted(line.split()[0] for line in lines)
        assert names == ["loop:self-improve", "loop:tick"]

    def test_no_owner_text_in_dim_anchors(self) -> None:
        """The verbose ``loop-owner=THIS session ✓`` line is gone (#1156)."""
        _make_lease("loop-owner", expires_in=timedelta(minutes=30), session_id="sess-A")

        lines = live_loops_anchor()

        joined = "\n".join(lines)
        assert "loop-owner=THIS session" not in joined, repr(joined)
        assert "loop-owner=unclaimed" not in joined, repr(joined)
        # Only the loop:<short> form survives.
        assert any(line.startswith("loop:owner") for line in lines), repr(lines)


@pytest.mark.django_db
class TestForeignHijackStillRed:
    """The #1073 foreign-hijack RED line is preserved through #1156."""

    def test_foreign_owner_still_emits_red_action_needed(self) -> None:
        """A foreign session owning ``loop-owner`` still routes to action_needed."""
        from teatree.core.managers import OwnershipStatus  # noqa: PLC0415
        from teatree.loop.statusline import loop_owner_anchor  # noqa: PLC0415

        status = OwnershipStatus(owner_session="foreign1", expires_at=None, is_live=True)
        zone, line = loop_owner_anchor(status, "this-session")

        assert zone == "action_needed", line
        assert "NOT this session" in line


@pytest.mark.django_db
class TestPopulateLoopsAnchorIntegration:
    """``_populate_loops_anchor`` emits live-loop dim lines + RED hijack only."""

    def test_emits_dim_line_per_live_loop_and_no_owner_verbose(self) -> None:
        from teatree.loop.tick import _populate_loops_anchor  # noqa: PLC0415

        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))

        zones = StatuslineZones()
        _populate_loops_anchor(zones)

        joined = "\n".join(item if isinstance(item, str) else item.text for item in zones.anchors)
        assert "loop:tick" in joined, repr(joined)
        assert "loop:self-improve" in joined, repr(joined)
        # Verbose dim owner lines must NOT appear.
        assert "loop-owner=THIS session" not in joined, repr(joined)
        assert "loop-owner=unclaimed" not in joined, repr(joined)
