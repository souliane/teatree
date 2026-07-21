"""Statusline per-session scoping of ``loop:<name>`` chunks (#1834 WI-2).

The dedicated-loop ``loop:<name>`` leases (e.g. ``loop:dispatch``) live in
their own namespace, disjoint from the infra leases (``loop-tick`` etc., which
use ``-`` not ``:``). The single shared loop line shows only the chunks for
the loops THIS session owns — a foreign session's ``loop:<name>`` lease is
subtracted so the user's statusline reflects their own loops, not the whole
machine. An anonymous/cron session (no resolvable id) fails open to the full
set so the line is never blanked. The single-owner default (no ``loop:<name>``
lease at all) is byte-identical to today.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.loop_lease_manager import is_per_loop_tick_mutex, per_loop_owner_slot
from teatree.core.models import Loop
from teatree.core.models.loop_lease import LoopLease
from teatree.loop.loop_scoping import (
    current_session_owned_per_loop_slots,
    is_transient_tick_mutex,
    loop_is_actively_ticking,
    owned_per_loop_slots,
    per_loop_loop_name,
)
from teatree.loop.statusline import live_loops_anchor
from teatree.loop.statusline_loops import _live_lease_chunks

_OWNED_SLOTS_TARGET = "teatree.loop.loop_scoping.owned_per_loop_slots"


def _at(seconds_ago: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds_ago)


class TestPerLoopChunkScoping:
    """``_live_lease_chunks`` drops ``loop:<name>`` chunks not owned by this session."""

    def test_infra_leases_unaffected_by_scoping(self) -> None:
        """The infra leases (``loop-tick`` etc.) are never per-loop-scoped — they always show."""
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", _at(120))]),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch(_OWNED_SLOTS_TARGET, return_value=set()),
        ):
            chunks = _live_lease_chunks()
        assert any(c.startswith("tick") for c in chunks), chunks

    def test_foreign_per_loop_chunk_subtracted(self) -> None:
        """A ``loop:<name>`` lease this session does NOT own is dropped from the line."""
        leases = [("loop-tick", _at(120)), ("loop:dispatch", _at(120)), ("loop:review", _at(120))]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            # This session owns only loop:dispatch.
            patch(_OWNED_SLOTS_TARGET, return_value={"loop:dispatch"}),
        ):
            chunks = _live_lease_chunks()
        joined = " · ".join(chunks)
        assert "tick" in joined, joined
        assert "loop:dispatch" in joined, joined
        # The foreign loop:review chunk is subtracted ...
        assert "loop:review" not in joined, joined

    def test_owned_per_loop_chunk_kept(self) -> None:
        leases = [("loop:dispatch", _at(120))]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch(_OWNED_SLOTS_TARGET, return_value={"loop:dispatch"}),
        ):
            chunks = _live_lease_chunks()
        assert any("loop:dispatch" in c for c in chunks), chunks

    def test_empty_session_fails_open_shows_all_per_loop_chunks(self) -> None:
        """No resolvable session ⇒ every ``loop:<name>`` chunk is kept (never blanked)."""
        leases = [("loop:dispatch", _at(120)), ("loop:review", _at(120))]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            # The sentinel ``None`` is the fail-open marker (no session / read error).
            patch(_OWNED_SLOTS_TARGET, return_value=None),
        ):
            chunks = _live_lease_chunks()
        joined = " · ".join(chunks)
        assert "loop:dispatch" in joined, joined
        assert "loop:review" in joined, joined

    def test_single_owner_default_chunks_byte_identical(self) -> None:
        """With no ``loop:<name>`` lease the chunk list is identical regardless of ownership read."""
        leases = [("loop-tick", _at(120)), ("loop-self-improve", _at(120))]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch(_OWNED_SLOTS_TARGET, return_value=set()),
        ):
            scoped = _live_lease_chunks()
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch(_OWNED_SLOTS_TARGET, return_value=None),
        ):
            failopen = _live_lease_chunks()
        assert scoped == failopen, (scoped, failopen)


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestOwnedPerLoopSlotsQuery:
    """``owned_per_loop_slots`` reads the real ``loop:<name>`` ownership from the DB."""

    def _seed(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(name="loop:dispatch", session_id="sess-A", lease_expires_at=now)
        LoopLease.objects.create(name="loop:review", session_id="sess-B", lease_expires_at=now)
        # Infra leases and the global owner are a different namespace (``-``).
        LoopLease.objects.create(name="loop-tick", owner="t", acquired_at=now)
        LoopLease.objects.create(name="t3-master", session_id="sess-A", lease_expires_at=now)

    def test_returns_only_this_sessions_per_loop_slots(self) -> None:
        self._seed()
        assert owned_per_loop_slots("sess-A") == {"loop:dispatch"}
        assert owned_per_loop_slots("sess-B") == {"loop:review"}

    def test_unknown_session_returns_empty_set_not_none(self) -> None:
        """A resolvable but non-owning session returns an empty set (subtract all), not the fail-open None."""
        self._seed()
        assert owned_per_loop_slots("sess-unknown") == set()

    def test_empty_session_is_fail_open_none(self) -> None:
        assert owned_per_loop_slots("") is None

    def test_db_read_error_fails_open_to_none(self) -> None:
        """A DB read error degrades to ``None`` (show all), never raises into the renderer."""
        with patch("django.apps.apps.get_model", side_effect=RuntimeError("db down")):
            assert owned_per_loop_slots("sess-A") is None

    def test_current_session_entry_point_resolves_the_active_session(self) -> None:
        """The renderer seam resolves ``current_session_id()`` and scopes to it."""
        self._seed()
        with patch("teatree.loop.session_identity.current_session_id", return_value="sess-A"):
            assert current_session_owned_per_loop_slots() == {"loop:dispatch"}

    def test_current_session_entry_point_fails_open_when_anonymous(self) -> None:
        self._seed()
        with patch("teatree.loop.session_identity.current_session_id", return_value=""):
            assert current_session_owned_per_loop_slots() is None


class TestLiveLoopsAnchorIntegration:
    """End-to-end: the composed loop line scopes ``loop:<name>`` chunks too."""

    def test_anchor_drops_foreign_per_loop_chunk(self) -> None:
        leases = [("loop-tick", _at(120)), ("loop:review", _at(120))]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch("teatree.loop.statusline_loops._mini_loop_schedules", return_value=[]),
            patch("teatree.loop.statusline_loops._waiting_count", return_value=0),
            patch(_OWNED_SLOTS_TARGET, return_value=set()),
        ):
            lines = live_loops_anchor()
        assert len(lines) == 1, repr(lines)
        assert "tick" in lines[0], lines[0]
        assert "loop:review" not in lines[0], lines[0]


class TestTransientTickMutexPredicate:
    """``loop-tick:<name>`` is the transient per-loop tick mutex; bare ``loop-tick`` is not."""

    def test_per_loop_tick_mutex_is_recognised(self) -> None:
        assert is_per_loop_tick_mutex("loop-tick:resource_pressure")
        assert is_transient_tick_mutex("loop-tick:resource_pressure")

    def test_bare_master_tick_mutex_is_not_a_per_loop_mutex(self) -> None:
        # The bare master ``loop-tick`` (no trailing ``:``) must stay visible as
        # ``tick`` — only the per-loop ``loop-tick:<name>`` variants are dropped.
        assert not is_per_loop_tick_mutex("loop-tick")
        assert not is_transient_tick_mutex("loop-tick")

    def test_owner_lease_is_not_a_tick_mutex(self) -> None:
        assert not is_transient_tick_mutex("loop:resource_pressure")


class TestTickMutexNotDoubleRendered:
    """A loop holding BOTH a ``loop-tick:<name>`` mutex and a ``loop:<name>`` lease renders once.

    The bug: the currently-ticking loop showed under two indistinguishable
    namespaces — the transient tick mutex (rendered ``tick:<name>`` after the
    ``loop-`` strip) AND its durable per-loop owner lease (``loop:<name>``) — so
    ``resource_pressure`` appeared twice on the t3-master line. The transient
    mutex must be dropped; the loop is represented by its ``loop:<name>`` chunk
    alone.
    """

    def test_currently_ticking_loop_is_not_shown_twice(self) -> None:
        # The render happens INSIDE the per-loop tick (`t3 loops tick --loop
        # resource_pressure`), so the `loop-tick:resource_pressure` mutex is live
        # alongside the durable `loop:resource_pressure` owner lease.
        leases = [
            ("loop-tick", _at(120)),
            ("loop-tick:resource_pressure", _at(120)),
            ("loop:resource_pressure", _at(120)),
        ]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch(_OWNED_SLOTS_TARGET, return_value={"loop:resource_pressure"}),
        ):
            chunks = _live_lease_chunks()
        joined = " · ".join(chunks)
        # The durable per-loop owner lease renders exactly once ...
        assert joined.count("loop:resource_pressure") == 1, joined
        # ... and the transient `tick:resource_pressure` mutex is NOT a second,
        # indistinguishable entry for the same loop.
        assert "tick:resource_pressure" not in joined, joined
        # The bare master `loop-tick` mutex still renders as `tick` (only the
        # per-loop mutex is dropped, never the master signal).
        assert any(c == "tick" or c.startswith("tick ") for c in chunks), chunks

    def test_tick_mutex_dropped_even_when_owner_lease_absent(self) -> None:
        # Defence in depth: a stray `loop-tick:<name>` with no matching
        # `loop:<name>` owner lease is still a transient mutex, never a chunk.
        leases = [("loop-tick:inbox", _at(120))]
        with (
            patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
            patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
            patch(_OWNED_SLOTS_TARGET, return_value=None),
        ):
            chunks = _live_lease_chunks()
        assert chunks == [], chunks


class TestPerLoopLoopName:
    def test_strips_the_owner_prefix(self) -> None:
        assert per_loop_loop_name("loop:inbox") == "inbox"

    def test_is_the_inverse_of_per_loop_owner_slot(self) -> None:
        assert per_loop_loop_name(per_loop_owner_slot("dispatch")) == "dispatch"

    def test_bare_name_without_prefix_is_unchanged(self) -> None:
        assert per_loop_loop_name("inbox") == "inbox"


class TestLoopIsActivelyTicking(TestCase):
    """``loop_is_actively_ticking`` reads the ``Loop`` cadence ledger (#3366)."""

    def _seed(self, **fields: object) -> None:
        Loop.objects.update_or_create(name="inbox", defaults={"script": "inbox", "enabled": True, **fields})

    def test_true_when_last_run_within_cadence_tolerance(self) -> None:
        self._seed(delay_seconds=60, last_run_at=timezone.now() - timedelta(seconds=90))
        assert loop_is_actively_ticking("loop:inbox") is True

    def test_false_when_last_run_beyond_cadence_tolerance(self) -> None:
        self._seed(delay_seconds=60, last_run_at=timezone.now() - timedelta(seconds=600))
        assert loop_is_actively_ticking("loop:inbox") is False

    def test_false_when_loop_never_ran(self) -> None:
        self._seed(delay_seconds=60, last_run_at=None)
        assert loop_is_actively_ticking("loop:inbox") is False

    def test_false_when_row_absent(self) -> None:
        assert loop_is_actively_ticking("loop:nonexistent") is False
