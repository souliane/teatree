"""The machine reading the suite pins, shared by the conftest fixture and its guard.

``read_machine_signal`` reads this box's free memory, its 1-minute load and its core
count, and every admission verdict is derived from all three — so an unpinned suite
lets the runner decide, and the failure surfaces in scanner and dispatch tests that
never mention machine capacity. Memory brakes at/under ``RAM_BRAKE_FLOOR_GB``; load
HALTs at/over ``BRAKE_LOAD_PER_CORE * cores``; the core count IS the WRITE ceiling
(``floor(cores * WRITE_CONCURRENCY_PER_CORE)``), so a 2-vCPU runner admits exactly one
concurrent dispatch and "two ticks claim two tasks" fails on a property of the runner.

Each value is an exact sentinel rather than merely a healthy one, so a guard asserting
it goes red on an unpinned probe in EVERY environment, not only on a starved box.
"""

from teatree.utils.ram_scope import RamHeadroom

PINNED_AVAILABLE_RAM_MIB = 64 * 1024

#: An idle box, well under ``BRAKE_LOAD_PER_CORE * PINNED_CORES``: the load component
#: contributes no pressure, so nothing rides on what else the runner is doing.
PINNED_LOAD1 = 0.25

#: Eight cores → a WRITE ceiling of 4, enough for the several-concurrent-claim tests to
#: measure the claim CAS rather than the runner's vCPU allocation.
PINNED_CORES = 8


def pinned_ram_headroom() -> RamHeadroom:
    """The pinned reading, scope-qualified so ``box_watermark_mib`` answers with it."""
    return RamHeadroom(
        available_mib=PINNED_AVAILABLE_RAM_MIB,
        cgroup_limit_mib=None,
        host_available_mib=PINNED_AVAILABLE_RAM_MIB,
    )


def pinned_load_and_cores() -> tuple[float, int]:
    return PINNED_LOAD1, PINNED_CORES
