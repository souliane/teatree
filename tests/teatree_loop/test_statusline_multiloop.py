"""Tests for the multi-loop anchors-zone rendering.

History:

*   Pre-#1156 the statusline rendered a verbose ``loop-owner=THIS session ✓``
    or ``loop-owner=unclaimed`` line plus the foreign-hijack RED line.
*   #1156 collapsed the dim doctrine into one line per LIVE LoopLease row
    (``loop:tick``, ``loop:owner``, …).
*   #1163 replaced that per-loop dump with a single consolidated summary
    line carrying time-to-next-tick.
*   #1400 re-expanded to one dedicated line per live loop, each with its
    own per-loop next-tick countdown — the consolidated count hid the
    per-loop schedule once multiple loops were configured.

The #1073 foreign-hijack RED line is preserved unchanged through every
refit — it is a different code path (``loop_owner_anchor``) and a
different zone (``action_needed``).
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
    """``live_loops_anchor()`` returns one dedicated line per live loop."""

    def test_one_line_per_live_loop(self) -> None:
        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))
        _make_lease("loop-slack-answer", expires_in=timedelta(minutes=30))

        lines = live_loops_anchor()

        assert len(lines) == 3, repr(lines)
        joined = "\n".join(lines)
        assert "loop-tick" in joined
        assert "loop-self-improve" in joined
        assert "loop-slack-answer" in joined

    def test_expired_loops_omitted(self) -> None:
        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))
        # Expired — must not appear in the output.
        _make_lease("loop-slack-answer", expires_in=timedelta(seconds=-5))

        lines = live_loops_anchor()

        assert len(lines) == 2, repr(lines)
        joined = "\n".join(lines)
        assert "loop-slack-answer" not in joined

    def test_per_loop_dump_format_gone(self) -> None:
        """The pre-#1156 verbose lines are still gone."""
        _make_lease("loop-owner", expires_in=timedelta(minutes=30), session_id="sess-A")
        _make_lease("loop-tick", expires_in=timedelta(minutes=30), session_id="sess-A")

        lines = live_loops_anchor()
        joined = "\n".join(lines)
        assert "loop-owner=THIS session" not in joined, repr(joined)
        assert "loop-owner=unclaimed" not in joined, repr(joined)
        # ``loop:owner`` / ``loop:tick`` colon-prefixed dump shape.
        assert "loop:tick" not in joined, repr(joined)
        assert "loop:owner" not in joined, repr(joined)

    def test_each_line_carries_next_tick_fragment(self) -> None:
        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))

        lines = live_loops_anchor()

        for line in lines:
            assert "next " in line, line


@pytest.mark.django_db
class TestForeignHijackStillRed:
    """The #1073 foreign-hijack RED line is preserved through every refit."""

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
    """The rendering layer emits one anchor line per live LoopLease row."""

    def test_emits_one_line_per_loop_when_loops_live(self) -> None:
        from teatree.loop.rendering import _populate_live_loops_anchor  # noqa: PLC0415

        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))

        zones = StatuslineZones()
        _populate_live_loops_anchor(zones)

        texts = [item if isinstance(item, str) else item.text for item in zones.anchors]
        joined = "\n".join(texts)
        assert "loop-tick" in joined, repr(joined)
        assert "loop-self-improve" in joined, repr(joined)
        # Verbose dim owner lines must NOT appear.
        assert "loop-owner=THIS session" not in joined, repr(joined)
        assert "loop-owner=unclaimed" not in joined, repr(joined)
        # Per-loop colon-prefixed dump form also gone.
        assert "loop:tick" not in joined, repr(joined)
