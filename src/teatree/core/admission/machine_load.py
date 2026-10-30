"""Machine load, core capacity, and host/container pressure readings for admission."""

import os

from teatree.core.admission_pressure import MachineSignal
from teatree.utils import host_pressure, ram_probe, ram_scope

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
    """Host-feed reading when fresh, else this container's own; an explicit *ram_available_gb* wins.

    Memory is the one scope-qualified cgroup-aware reader (#4118, #4217): the absolute box-wide
    floors never judge a cgroup too small to host an agent (#4252), and ``None`` (unreadable) stays
    distinct from ``0`` (readable, nothing left).
    """
    host = host_pressure.read_host_pressure()
    if host is not None:
        memory_cap_gb = None
        memory_source = "" if ram_available_gb is not None else "host feed"
        if ram_available_gb is None:
            available_mib = host.ram_available_mib
            headroom = ram_scope.read_ram_headroom()
            if headroom.cgroup_is_box_scoped:
                if headroom.available_mib is not None:
                    if headroom.available_mib < available_mib:
                        memory_source = "container reading"
                    available_mib = min(available_mib, headroom.available_mib)
                memory_cap_gb = headroom.box_watermark_cap_gb
            ram_available_gb = available_mib / _MIB_PER_GB
        return MachineSignal(
            cores=min(host.cores, ram_probe.available_cpu_count()),
            load1=host.load1,
            ram_available_gb=ram_available_gb,
            memory_cap_gb=memory_cap_gb,
            swap_mib_per_s=host.swap_mib_per_s,
            vm_pressure_level=host.vm_pressure_level,
            load_cores=host.cores,
            load_brake_per_core=brake_per_core,
            load_resume_per_core=resume_per_core,
            source="host feed",
            memory_source=memory_source,
        )
    load1, cores = read_load_and_cores()
    # The cap rides only on our own reading: a caller's figure has an unknown scope.
    memory_cap_gb = None
    memory_source = "" if ram_available_gb is not None else "container reading"
    if ram_available_gb is None:
        headroom = ram_scope.read_ram_headroom()
        available_mib = headroom.box_watermark_mib
        ram_available_gb = None if available_mib is None else available_mib / _MIB_PER_GB
        memory_cap_gb = headroom.box_watermark_cap_gb
    return MachineSignal(
        cores=cores,
        load1=load1,
        ram_available_gb=ram_available_gb,
        memory_cap_gb=memory_cap_gb,
        load_brake_per_core=brake_per_core,
        load_resume_per_core=resume_per_core,
        source="container reading",
        memory_source=memory_source,
    )
