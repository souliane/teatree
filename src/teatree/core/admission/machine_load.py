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
    """The deterministic, model-free machine probe (stdlib only, no external process).

    Load and cores come from :func:`~teatree.core.admission.machine_load.read_load_and_cores`,
    a named seam so the whole signal is pinnable rather than only its memory third.

    Memory comes from :attr:`~teatree.utils.ram_scope.RamHeadroom.box_watermark_mib`, the ONE
    cgroup-aware reader. Reading ``/proc/meminfo`` here instead would report the HOST figure
    inside a container and would be invisible to that reader's own fix (#4118).

    That reading is SCOPE-QUALIFIED, which the watermarks this signal feeds require (#4217):
    :data:`RAM_BRAKE_FLOOR_GB` and :data:`RAM_RESUME_FLOOR_GB` are absolute box-wide numbers,
    so a reading taken in a cgroup too small to host an agent workload is not theirs to judge
    — the admin sidecar read 1.65 GB at the same instant the worker read 15.88 GB, and its
    fixed 2 GiB cap put every dispatch under a floor it could never rise above. Such a scope
    falls back to the host component, which is box-wide at any cap, so a genuinely starved box
    still brakes from inside a sidecar (#4252). ``None`` (nothing box-scoped readable) is a
    different answer from ``0`` (readable, nothing left), carried through rather than collapsed
    to a number nobody measured.

    An explicit *ram_available_gb* wins, so a caller holding a reading of its own is never
    made to pay for a second probe — which is also how a caller wanting only the load
    average gets it without a second ``/proc`` read, since this is its one reader. A
    platform with no load average reads ``0.0``: an unknown load is inert wherever it is
    consumed (no brake, full :func:`box_load_headroom`), never a manufactured clamp.
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
            cores=(
                min(host.cores, ram_probe.available_cpu_count(), quota)
                if (quota := ram_probe.cgroup_cpu_quota()) is not None
                else min(host.cores, ram_probe.available_cpu_count())
            ),
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
    # The cap rides along only on OUR OWN reading: a caller's figure has an unknown scope,
    # and a cap paired with the wrong scope diagnoses the wrong container.
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
