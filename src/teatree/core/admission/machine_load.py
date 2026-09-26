"""This box's load and core count, the two ``os`` readings admission reads, behind one pinnable seam."""

import os

from teatree.utils import ram_probe


def read_load_and_cores() -> tuple[float, int]:
    """This box's 1-minute load and its core count — the two ``os`` readings, one seam.

    Named for the reason :func:`~teatree.utils.ram_scope.read_ram_headroom` is: the suite
    has to be able to pin them. Unpinned they decide admission for every test that never
    mentions machine capacity — a busy box HALTs at ``BRAKE_LOAD_PER_CORE * cores``, and
    the core count IS the WRITE ceiling (``admission_governor._machine_ceiling``), so a 2-vCPU runner
    admits exactly one concurrent dispatch. A platform with no load average reads ``0.0``:
    an unknown load is inert wherever it is consumed, never a manufactured clamp.
    """
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    return load1, ram_probe.available_cpu_count()
