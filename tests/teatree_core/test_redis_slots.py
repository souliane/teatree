"""Redis DB slot allocator.

Teatree runs a single shared Redis container (`teatree-redis`) on localhost:6379.
Each ticket gets a unique Redis DB index so cache/celery keys don't collide
across tickets. Slot count is configurable via ``teatree.redis_db_count`` in
``~/.teatree.toml`` (default 16). When every slot is taken, allocation raises
RedisSlotsExhaustedError. Slots are released on ticket cleanup (FLUSHDB + clear field).
"""

import tempfile
from dataclasses import replace as _replace
from pathlib import Path
from unittest.mock import ANY, patch

import pytest
from django.test import TestCase

from teatree.config import load_config
from teatree.core.models import Ticket
from teatree.core.models.errors import RedisSlotsExhaustedError
from teatree.core.models.worktree import Worktree

REDIS_DB_COUNT = load_config().user.redis_db_count


def _make_live_ticket() -> tuple[Ticket, Path]:
    """Create a ticket with an allocated slot backed by a real on-disk directory."""
    tmpdir = Path(tempfile.mkdtemp())
    ticket = Ticket.objects.create()
    Ticket.objects.allocate_redis_slot(ticket)
    Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path="org/repo",
        branch="main",
        extra={"worktree_path": str(tmpdir)},
    )
    return ticket, tmpdir


class TestAllocateRedisSlot(TestCase):
    def test_allocates_lowest_free_index(self) -> None:
        ticket = Ticket.objects.create()
        index = Ticket.objects.allocate_redis_slot(ticket)
        assert index == 0
        ticket.refresh_from_db()
        assert ticket.redis_db_index == 0

    def test_next_allocation_skips_taken_slots(self) -> None:
        first = Ticket.objects.create()
        Ticket.objects.allocate_redis_slot(first)
        second = Ticket.objects.create()
        assert Ticket.objects.allocate_redis_slot(second) == 1

    def test_reuses_released_slot(self) -> None:
        first = Ticket.objects.create()
        Ticket.objects.allocate_redis_slot(first)
        second = Ticket.objects.create()
        Ticket.objects.allocate_redis_slot(second)
        with patch("teatree.utils.redis_container.flushdb"):
            first.release_redis_slot()
        third = Ticket.objects.create()
        assert Ticket.objects.allocate_redis_slot(third) == 0

    def test_returns_existing_slot_if_already_allocated(self) -> None:
        ticket = Ticket.objects.create()
        first = Ticket.objects.allocate_redis_slot(ticket)
        second = Ticket.objects.allocate_redis_slot(ticket)
        assert first == second

    def test_raises_when_all_live_slots_are_in_use(self) -> None:
        live_tickets = []
        live_dirs = []
        for _ in range(REDIS_DB_COUNT):
            ticket, tmpdir = _make_live_ticket()
            live_tickets.append(ticket)
            live_dirs.append(tmpdir)
        overflow = Ticket.objects.create()
        try:
            with pytest.raises(RedisSlotsExhaustedError):
                Ticket.objects.allocate_redis_slot(overflow)
        finally:
            for d in live_dirs:
                d.rmdir()

    def test_slot_count_is_configurable(self) -> None:
        cfg = load_config()
        patched_user = _replace(cfg.user, redis_db_count=2)
        patched_cfg = _replace(cfg, user=patched_user)
        with patch("teatree.core.managers.load_config", return_value=patched_cfg):
            _a_ticket, a_dir = _make_live_ticket()
            _b_ticket, b_dir = _make_live_ticket()
            overflow = Ticket.objects.create()
            try:
                with pytest.raises(RedisSlotsExhaustedError):
                    Ticket.objects.allocate_redis_slot(overflow)
            finally:
                a_dir.rmdir()
                b_dir.rmdir()


class TestGhostSlotReclaim(TestCase):
    """``allocate_redis_slot`` auto-reclaims ghosts before raising exhaustion.

    A ghost is a ticket whose ``redis_db_index`` is set but all its Worktree
    rows have ``worktree_path`` values that no longer exist on disk (or it has
    no Worktree rows at all — a failed provision that allocated then deleted
    without releasing). The allocator must reclaim those before raising
    ``RedisSlotsExhaustedError``, so exhaustion-from-leaks cannot occur without
    manual intervention.

    Anti-vacuity: with the old code (no ghost reclaim in ``allocate_redis_slot``),
    tests that try to allocate past a ghost-filled cap go RED — the overflow
    allocation raises ``RedisSlotsExhaustedError`` even though ghost slots are
    present. Restoring the reclaim makes them GREEN.
    """

    def _fill_slots_with_live_worktrees(self, count: int) -> tuple[list[Ticket], list[Path]]:
        """Allocate ``count`` slots, each backed by a real on-disk directory."""
        dirs: list[Path] = []
        tickets: list[Ticket] = []
        for _ in range(count):
            ticket, tmpdir = _make_live_ticket()
            tickets.append(ticket)
            dirs.append(tmpdir)
        return tickets, dirs

    def test_zero_worktree_tickets_are_not_reclaimed_as_ghosts(self) -> None:
        cfg = load_config()
        patched_user = _replace(cfg.user, redis_db_count=2)
        patched_cfg = _replace(cfg, user=patched_user)
        with patch("teatree.core.managers.load_config", return_value=patched_cfg):
            no_wt_a = Ticket.objects.create()
            no_wt_b = Ticket.objects.create()
            Ticket.objects.allocate_redis_slot(no_wt_a)
            Ticket.objects.allocate_redis_slot(no_wt_b)
            newcomer = Ticket.objects.create()
            with pytest.raises(RedisSlotsExhaustedError):
                Ticket.objects.allocate_redis_slot(newcomer)

    def test_allocation_succeeds_when_ghost_slots_have_missing_on_disk_path(self) -> None:
        cfg = load_config()
        patched_user = _replace(cfg.user, redis_db_count=2)
        patched_cfg = _replace(cfg, user=patched_user)
        with patch("teatree.core.managers.load_config", return_value=patched_cfg):
            ghost_a = Ticket.objects.create()
            ghost_b = Ticket.objects.create()
            Ticket.objects.allocate_redis_slot(ghost_a)
            Ticket.objects.allocate_redis_slot(ghost_b)
            for ghost in (ghost_a, ghost_b):
                Worktree.objects.create(
                    ticket=ghost,
                    overlay="test",
                    repo_path="org/repo",
                    branch="main",
                    extra={"worktree_path": "/nonexistent/path/that/is/gone"},
                )
            newcomer = Ticket.objects.create()
            index = Ticket.objects.allocate_redis_slot(newcomer)
        assert index in {0, 1}
        newcomer.refresh_from_db()
        assert newcomer.redis_db_index == index

    def test_live_non_ghost_slot_is_never_reclaimed(self) -> None:
        cfg = load_config()
        patched_user = _replace(cfg.user, redis_db_count=2)
        patched_cfg = _replace(cfg, user=patched_user)
        with patch("teatree.core.managers.load_config", return_value=patched_cfg):
            live_tickets, live_dirs = self._fill_slots_with_live_worktrees(2)
            overflow = Ticket.objects.create()
            with pytest.raises(RedisSlotsExhaustedError):
                Ticket.objects.allocate_redis_slot(overflow)
            for live in live_tickets:
                live.refresh_from_db()
                assert live.redis_db_index is not None
        for d in live_dirs:
            d.rmdir()

    def test_mixed_live_and_ghost_slots_reclaims_only_ghosts(self) -> None:
        cfg = load_config()
        patched_user = _replace(cfg.user, redis_db_count=3)
        patched_cfg = _replace(cfg, user=patched_user)
        with patch("teatree.core.managers.load_config", return_value=patched_cfg):
            live_tickets, live_dirs = self._fill_slots_with_live_worktrees(2)
            live_indices_before = {t.redis_db_index for t in live_tickets}
            ghost = Ticket.objects.create()
            Ticket.objects.allocate_redis_slot(ghost)
            Worktree.objects.create(
                ticket=ghost,
                overlay="test",
                repo_path="org/repo",
                branch="main",
                extra={"worktree_path": "/gone/path"},
            )
            newcomer = Ticket.objects.create()
            index = Ticket.objects.allocate_redis_slot(newcomer)
        assert index not in live_indices_before
        for live in live_tickets:
            live.refresh_from_db()
            assert live.redis_db_index in live_indices_before
        for d in live_dirs:
            d.rmdir()

    def test_ghost_slots_cleared_in_db_after_reclaim(self) -> None:
        cfg = load_config()
        patched_user = _replace(cfg.user, redis_db_count=2)
        patched_cfg = _replace(cfg, user=patched_user)
        with patch("teatree.core.managers.load_config", return_value=patched_cfg):
            ghost = Ticket.objects.create()
            Ticket.objects.allocate_redis_slot(ghost)
            Worktree.objects.create(
                ticket=ghost,
                overlay="test",
                repo_path="org/repo",
                branch="main",
                extra={"worktree_path": "/nonexistent/ghost/path"},
            )
            assert ghost.redis_db_index is not None
            _filler_ticket, filler_dir = _make_live_ticket()
            newcomer = Ticket.objects.create()
            Ticket.objects.allocate_redis_slot(newcomer)
            ghost.refresh_from_db()
            assert ghost.redis_db_index is None
        filler_dir.rmdir()


class TestGhostSlotReclaimFlushes(TestCase):
    """``_reclaim_ghost_slots`` must FLUSHDB each ghost slot before clearing the field.

    Anti-vacuity: remove the ``redis_container.flushdb`` call from
    ``_reclaim_ghost_slots`` and this test goes RED — ``mock_flush`` records
    zero calls. Restoring the flush makes it GREEN.
    """

    def test_reclaim_flushes_ghost_slot_db(self) -> None:
        cfg = load_config()
        patched_user = _replace(cfg.user, redis_db_count=2)
        patched_cfg = _replace(cfg, user=patched_user)
        with patch("teatree.core.managers.load_config", return_value=patched_cfg):
            ghost = Ticket.objects.create()
            ghost_index = Ticket.objects.allocate_redis_slot(ghost)
            Worktree.objects.create(
                ticket=ghost,
                overlay="test",
                repo_path="org/repo",
                branch="main",
                extra={"worktree_path": "/nonexistent/ghost/path"},
            )
            _filler, filler_dir = _make_live_ticket()
            newcomer = Ticket.objects.create()
            with patch("teatree.core.managers.redis_container.flushdb") as mock_flush:
                Ticket.objects.allocate_redis_slot(newcomer)
        mock_flush.assert_called_once_with(ghost_index, db_count=patched_user.redis_db_count)
        filler_dir.rmdir()


class TestReleaseRedisSlot(TestCase):
    def test_clears_field_and_flushes_redis_db(self) -> None:
        ticket = Ticket.objects.create()
        index = Ticket.objects.allocate_redis_slot(ticket)
        with patch("teatree.utils.redis_container.flushdb") as mock_flush:
            ticket.release_redis_slot()
        mock_flush.assert_called_once_with(index, db_count=ANY)
        ticket.refresh_from_db()
        assert ticket.redis_db_index is None

    def test_release_is_idempotent_when_no_slot_allocated(self) -> None:
        ticket = Ticket.objects.create()
        with patch("teatree.utils.redis_container.flushdb") as mock_flush:
            ticket.release_redis_slot()
        mock_flush.assert_not_called()
        assert ticket.redis_db_index is None
