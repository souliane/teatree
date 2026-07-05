"""The per-loop tick-driver chip on the statusline loop line (PR-26 / M9).

An owned ``loop:<name>`` chunk renders ``·<driver>`` (or ``·DRIVERLESS`` when the
slot is owned but no driver is registered). Only the pid-anchored ``loop:<name>``
ownership layer carries the chip — an infra lease (``loop-reinstall`` and friends)
renders neither, pinning edge-case 6.
"""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from teatree.loop.statusline_loops import _live_lease_chunks


def _at(seconds_ago: int):
    return timezone.now() - timedelta(seconds=seconds_ago)


def _chunks(leases, drivers):
    with (
        patch("teatree.loop.statusline_loops._live_loop_leases", return_value=leases),
        patch("teatree.loop.statusline_loops._live_lease_drivers", return_value=drivers),
        patch("teatree.loop.statusline_loops.current_session_owned_per_loop_slots", return_value=None),
    ):
        return _live_lease_chunks(colorize=False)


class TestPerLoopDriverChip:
    def test_owned_per_loop_slot_with_blank_driver_renders_driverless(self) -> None:
        chunks = _chunks([("loop:dispatch", _at(30))], {"loop:dispatch": ("sess-a", "")})
        assert any("DRIVERLESS" in c for c in chunks)

    def test_owned_per_loop_slot_with_driver_renders_the_suffix(self) -> None:
        chunks = _chunks([("loop:dispatch", _at(30))], {"loop:dispatch": ("sess-a", "loop_runner")})
        assert any("·loop_runner" in c for c in chunks)
        assert not any("DRIVERLESS" in c for c in chunks)

    def test_unowned_per_loop_slot_renders_no_chip(self) -> None:
        # A live lease row with no session owner (blank session_id) is not
        # DRIVERLESS — there is no owner to warn.
        chunks = _chunks([("loop:dispatch", _at(30))], {"loop:dispatch": ("", "")})
        assert not any("DRIVERLESS" in c for c in chunks)
        assert not any("·" in c for c in chunks)

    def test_infra_lease_renders_no_driver_chip(self) -> None:
        chunks = _chunks([("loop-reinstall", _at(30))], {"loop-reinstall": ("sess-a", "")})
        assert chunks
        assert not any("DRIVERLESS" in c for c in chunks)
        assert not any("·" in c for c in chunks)
