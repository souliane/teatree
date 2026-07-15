"""Tests for the multi-loop anchors-zone rendering.

History:

*   Pre-#1156 the statusline rendered a verbose ``t3-master=THIS session ✓``
    or ``t3-master=unclaimed`` line plus the foreign-hijack RED line.
*   #1156 collapsed the dim doctrine into one line per LIVE LoopLease row
    (``loop:tick``, ``loop:owner``, …).
*   A later refit replaced that per-loop dump with a single consolidated
    ``loop · next tick in <d> · N loops live`` summary line.
*   A later refit dropped the useless ``N loops live`` count: each live
    loop lists its short name + next tick as a relative duration in
    minutes. The user explicitly opted out of the bare count.
*   #130 added a leading state word, later dropped as redundant with the
    ``tick <next-tick>`` chunk (both derive from the same live-lease set),
    so the line now leads with the loop chunks themselves
    (``tick 11m · tickets 11m``) and appends a ``waiting=N``
    clause when the loop is blocked on the user.

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


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestLiveLoopsAnchor:
    """``live_loops_anchor()`` returns one per-loop-named summary line."""

    def test_one_line_lists_each_live_loop_by_name(self) -> None:
        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))
        _make_lease("loop-slack-answer", expires_in=timedelta(minutes=30))

        lines = live_loops_anchor()

        assert len(lines) == 1, repr(lines)
        line = lines[0]
        # The redundant leading state word is gone — the line leads with a
        # loop chunk (leases sort by name → ``self-improve`` first here).
        assert "loop running" not in line, line
        assert line.startswith("self-improve"), line
        # The useless headline count is gone; each loop's short name appears.
        assert "loops live" not in line, line
        assert "tick" in line, line
        assert "self-improve" in line, line
        assert "slack-answer" in line, line

    def test_expired_loops_omitted(self) -> None:
        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))
        # Expired — must not appear in the line.
        _make_lease("loop-slack-answer", expires_in=timedelta(seconds=-5))

        lines = live_loops_anchor()

        assert len(lines) == 1, repr(lines)
        assert "slack-answer" not in lines[0], lines[0]
        assert "tick" in lines[0], lines[0]
        assert "self-improve" in lines[0], lines[0]

    def test_per_loop_dump_format_gone(self) -> None:
        """The pre-refit per-loop dump (``loop:tick`` / ``loop:owner``) is gone."""
        _make_lease("t3-master", expires_in=timedelta(minutes=30), session_id="sess-A")
        _make_lease("loop-tick", expires_in=timedelta(minutes=30), session_id="sess-A")

        lines = live_loops_anchor()
        joined = "\n".join(lines)
        # The verbose pre-#1156 lines are still gone.
        assert "t3-master=THIS session" not in joined, repr(joined)
        assert "t3-master=unclaimed" not in joined, repr(joined)
        # The per-loop dump (``loop:tick`` / ``loop:owner``) is gone too.
        assert "loop:tick" not in joined, repr(joined)
        assert "loop:owner" not in joined, repr(joined)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestForeignHijackStillRed:
    """The #1073 foreign-hijack RED line is preserved through every refit."""

    def test_foreign_owner_still_emits_red_action_needed(self) -> None:
        """A foreign session owning ``t3-master`` still routes to action_needed."""
        from teatree.core.managers import OwnershipStatus  # noqa: PLC0415
        from teatree.loop.statusline import loop_owner_anchor  # noqa: PLC0415

        status = OwnershipStatus(owner_session="foreign1", expires_at=None, is_live=True)
        zone, line = loop_owner_anchor(status, "this-session")

        assert zone == "action_needed", line
        assert "NOT this session" in line


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestPopulateLoopsAnchorIntegration:
    """The rendering layer emits the consolidated line for live LoopLease rows."""

    def test_emits_consolidated_line_when_loops_live(self) -> None:
        from teatree.loop.rendering import _populate_dashboard_head  # noqa: PLC0415

        _make_lease("loop-tick", expires_in=timedelta(minutes=30))
        _make_lease("loop-self-improve", expires_in=timedelta(minutes=30))

        zones = StatuslineZones()
        _populate_dashboard_head(zones)

        joined = "\n".join(item if isinstance(item, str) else item.text for item in zones.anchors)
        assert "tick" in joined, repr(joined)
        # Each live loop is named; the bare count is gone.
        assert "loops live" not in joined, repr(joined)
        assert "tick" in joined, repr(joined)
        assert "self-improve" in joined, repr(joined)
        # Verbose dim owner lines must NOT appear.
        assert "t3-master=THIS session" not in joined, repr(joined)
        assert "t3-master=unclaimed" not in joined, repr(joined)
        # Per-loop dump form also gone.
        assert "loop:tick" not in joined, repr(joined)
