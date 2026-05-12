"""Redis DB slot allocator.

Teatree runs a single shared Redis container (`teatree-redis`) on localhost:6379.
Each ticket gets a unique Redis DB index so cache/celery keys don't collide
across tickets. Slot count is configurable via ``teatree.redis_db_count`` in
``~/.teatree.toml`` (default 16). When every slot is taken, allocation raises
RedisSlotsExhaustedError. Slots are released on ticket cleanup (FLUSHDB + clear field).
"""

from dataclasses import replace as _replace
from unittest.mock import ANY, patch

import pytest
from django.test import TestCase

from teatree.config import load_config
from teatree.core.models import Ticket
from teatree.core.models.errors import RedisSlotsExhaustedError

REDIS_DB_COUNT = load_config().user.redis_db_count


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

    def test_raises_when_all_slots_are_in_use(self) -> None:
        tickets = [Ticket.objects.create() for _ in range(REDIS_DB_COUNT)]
        for ticket in tickets:
            Ticket.objects.allocate_redis_slot(ticket)
        overflow = Ticket.objects.create()
        with pytest.raises(RedisSlotsExhaustedError):
            Ticket.objects.allocate_redis_slot(overflow)

    def test_slot_count_is_configurable(self) -> None:
        cfg = load_config()
        patched_user = _replace(cfg.user, redis_db_count=2)
        patched_cfg = _replace(cfg, user=patched_user)
        with patch("teatree.core.managers.load_config", return_value=patched_cfg):
            a = Ticket.objects.create()
            b = Ticket.objects.create()
            Ticket.objects.allocate_redis_slot(a)
            Ticket.objects.allocate_redis_slot(b)
            overflow = Ticket.objects.create()
            with pytest.raises(RedisSlotsExhaustedError):
                Ticket.objects.allocate_redis_slot(overflow)


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
