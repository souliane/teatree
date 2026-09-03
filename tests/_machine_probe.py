"""The memory reading the suite pins, shared by the conftest fixture and its guard.

``read_machine_signal`` reads live memory and the governor brakes at/under
``RAM_BRAKE_FLOOR_GB``, so an unpinned suite denies admission on any box that happens
to be under memory pressure — and the failure surfaces in scanner and dispatch tests
that never mention memory. The value is an exact sentinel rather than merely a large
one so a guard asserting it goes red on an unpinned probe in EVERY environment, not
only on a starved one.
"""

from teatree.utils.ram_scope import RamHeadroom

PINNED_AVAILABLE_RAM_MIB = 64 * 1024


def pinned_ram_headroom() -> RamHeadroom:
    """The pinned reading, scope-qualified so ``box_watermark_mib`` answers with it."""
    return RamHeadroom(
        available_mib=PINNED_AVAILABLE_RAM_MIB,
        cgroup_limit_mib=None,
        host_available_mib=PINNED_AVAILABLE_RAM_MIB,
    )
