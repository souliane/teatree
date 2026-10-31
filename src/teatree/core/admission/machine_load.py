"""Machine load, core capacity and memory readings for admission."""

import os

from teatree.core.admission_pressure import MachineSignal
from teatree.utils import ram_probe, ram_scope

_MIB_PER_GB = 1024.0


def read_load_and_cores() -> tuple[float, int]:
    """This box's 1-minute load and its core count — the two ``os`` readings, one seam.

    Named for the reason :func:`~teatree.utils.ram_scope.read_ram_headroom` is: the suite
    has to be able to pin them. Unpinned they decide admission for every test that never
    mentions machine capacity — a busy box HALTs at ``BRAKE_LOAD_PER_CORE * cores``, and
    the core count IS the WRITE ceiling (``write_ceiling.AdmissionCeiling.machine``), so at the
    shipped factor a 2-vCPU runner admits one concurrent dispatch. A platform with no load
    average reads ``0.0``: an unknown load is inert wherever it is consumed, never a
    manufactured clamp.
    """
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    return load1, ram_probe.available_cpu_count()


def read_machine_signal(
    *, ram_available_gb: float | None = None, brake_per_core: float, resume_per_core: float
) -> MachineSignal:
    """This box's load and memory, read from ``/proc`` and the cgroup; an explicit *ram_available_gb* wins.

    ``/proc/meminfo`` and ``/proc/loadavg`` are host-wide inside every container, so the figures
    need no feed between processes. Load is judged against the HOST's cores while ``cores`` stays
    what this process may use. Memory is the one scope-qualified reader (#4118, #4217): a cgroup
    too small to host an agent never judges the box (#4252), and an unreadable ``/proc/meminfo``
    is ``None`` — unknown, never a cgroup figure — which stays distinct from ``0`` (nothing left).
    """
    load1, cores = read_load_and_cores()
    memory_cap_gb = None
    memory_source = "" if ram_available_gb is not None else "container reading"
    if ram_available_gb is None:
        headroom = ram_scope.read_ram_headroom()
        available_mib = None if headroom.host_available_mib is None else headroom.box_watermark_mib
        ram_available_gb = None if available_mib is None else available_mib / _MIB_PER_GB
        memory_cap_gb = headroom.box_watermark_cap_gb
    return MachineSignal(
        cores=cores,
        load1=load1,
        ram_available_gb=ram_available_gb,
        memory_cap_gb=memory_cap_gb,
        load_cores=os.cpu_count(),
        load_brake_per_core=brake_per_core,
        load_resume_per_core=resume_per_core,
        source="container reading",
        memory_source=memory_source,
    )
