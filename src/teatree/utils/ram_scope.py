"""Cgroup-v2 memory accounting, and the SCOPE a memory reading is valid in (#4217).

:mod:`teatree.utils.ram_probe` answers "how much memory is available to this process".
This module answers the question that has to be settled before that number may be
compared against anything absolute: *whose* memory — the box's, or one small sidecar's?

Two defects lived in the single unscoped answer. A reading taken in the 2 GiB admin
container reported 1.65 GB free at the same instant the worker read 15.88 GB and the host
had 22 GB free, so the box-wide watermarks in :mod:`teatree.core.admission_governor`
denied every dispatch made from the container interactive work actually runs in — and
denied it permanently, since a fixed 2 GiB cap can never rise above a 4 GB floor. And
cgroup-v2's ``memory.current`` CHARGES page cache, which the kernel hands straight back
under pressure, so ``memory.max - memory.current`` under-reports by however much cache a
day of test suites left behind. ``MemAvailable`` credits exactly that cache host-side;
the cgroup arm now does the same, from one reader both consumers share.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.ram_probe import cgroup_v2_memory_mib, host_available_ram_mib

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_MIB_PER_GIB = 1024
_BYTES_PER_MIB = 1024 * 1024

#: The cgroup ceiling below which a container cannot host an agent workload at all, so a
#: reading taken inside one describes that container and never the box. Same default and
#: same env override as ``t3 doctor``'s worker-cap FAIL, which is where the floor is
#: stated to operators — one number, so the guard and the scope test cannot disagree.
DEFAULT_AGENT_WORKLOAD_FLOOR_GIB = 4
AGENT_WORKLOAD_FLOOR_ENV = "TEATREE_WORKER_MEMORY_FLOOR_GIB"

#: ``memory.stat`` keys the kernel reclaims on demand, mirroring what ``MemAvailable``
#: credits host-side. Deliberately not ``file``: active file pages come back only under
#: real pressure, and crediting them would overstate headroom.
_RECLAIMABLE_STAT_KEYS = frozenset({"inactive_file", "slab_reclaimable"})


def agent_workload_floor_gib(raw: str | None) -> int:
    """Parse *raw* into the floor in whole GiB; the default on absent/garbage/non-positive.

    Pure — the caller reads :data:`AGENT_WORKLOAD_FLOOR_ENV` — so an operator typo
    falls back to a real floor instead of silently disabling one.
    """
    if raw is None:
        return DEFAULT_AGENT_WORKLOAD_FLOOR_GIB
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_AGENT_WORKLOAD_FLOOR_GIB
    return value if value > 0 else DEFAULT_AGENT_WORKLOAD_FLOOR_GIB


def cgroup_v2_reclaimable_mib() -> int:
    """Reclaimable page cache + slab charged to this cgroup, in whole MiB; ``0`` when unreadable.

    ``0`` on any failure keeps the uncorrected arithmetic rather than crediting memory
    nobody measured — under-reporting headroom is the safe direction.
    """
    try:
        raw = (_CGROUP_ROOT / "memory.stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    total = 0
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        stripped = value.strip()
        if key in _RECLAIMABLE_STAT_KEYS and stripped.isdigit():
            total += int(stripped)
    return total // _BYTES_PER_MIB


def cgroup_headroom_mib() -> int | None:
    """What this cgroup can still allocate, in whole MiB; ``None`` when it has no cap.

    ``memory.max - (memory.current - reclaimable)`` — the cgroup mirror of the host's
    ``MemAvailable``. Without the correction a day of ``-n auto`` suites leaves
    ``memory.current`` sitting near ``memory.max`` as pure page cache, and every reader
    of this number concludes the box is full.
    """
    return _cgroup_cap_and_headroom_mib()[1]


def _cgroup_cap_and_headroom_mib() -> tuple[int | None, int | None]:
    """This cgroup's ``memory.max`` and its remaining headroom, both in whole MiB.

    Read together because :func:`read_ram_headroom` needs both, and a second read of
    ``memory.max`` would let the cap and the headroom describe different instants.
    """
    limit_mib = cgroup_v2_memory_mib("memory.max")
    current_mib = cgroup_v2_memory_mib("memory.current")
    if limit_mib is None or current_mib is None:
        return limit_mib, None
    return limit_mib, max(0, limit_mib - max(0, current_mib - cgroup_v2_reclaimable_mib()))


@dataclass(frozen=True, slots=True)
class RamHeadroom:
    """Available RAM, carrying both the cgroup cap it was measured against and its box-scoped half.

    The combined number alone is unjudgeable: identical arithmetic in a 2 GiB sidecar and on
    the uncapped host produce readings an absolute watermark cannot tell apart. The host
    component is kept beside it because that one IS box-scoped at any cap — ``/proc/meminfo``
    inside a container reports the host — so an out-of-scope reading has an answer to fall
    back on rather than nothing at all (#4252).
    """

    available_mib: int | None
    cgroup_limit_mib: int | None
    host_available_mib: int | None

    @property
    def box_watermark_mib(self) -> int | None:
        """The reading box-wide watermarks may judge; ``None`` is UNKNOWN, which never brakes.

        A cgroup too small to host an agent workload describes that container and never the
        box, so its arithmetic is dropped and the host component answers alone. ``None``
        survives only when nothing box-scoped was readable — never as the verdict on a scope,
        which is the permanent brake a fixed cap could never release (#4217).
        """
        floor_mib = agent_workload_floor_gib(os.environ.get(AGENT_WORKLOAD_FLOOR_ENV)) * _MIB_PER_GIB
        if self.cgroup_limit_mib is not None and self.cgroup_limit_mib < floor_mib:
            return self.host_available_mib
        return self.available_mib


def read_ram_headroom() -> RamHeadroom:
    """RAM available to THIS process — the minimum of every scope that can answer (#3992).

    ``/proc/meminfo`` inside a container reports the HOST's memory, so a capped worker
    would otherwise size work against headroom it may not touch. Both components survive
    onto the struct: ``min()`` is what THIS process may allocate, and collapsing to it
    before the scope test threw away the only figure a sidecar could still be judged on.

    ``None`` (nothing readable) stays a different answer from ``0`` (readable, nothing
    left): a caller falls back on the first and tightens on the second.
    """
    limit_mib, headroom_mib = _cgroup_cap_and_headroom_mib()
    host = host_available_ram_mib()
    host_mib = host if host > 0 else None
    candidates = [mib for mib in (host_mib, headroom_mib) if mib is not None]
    return RamHeadroom(
        available_mib=min(candidates) if candidates else None,
        cgroup_limit_mib=limit_mib,
        host_available_mib=host_mib,
    )


__all__ = [
    "AGENT_WORKLOAD_FLOOR_ENV",
    "DEFAULT_AGENT_WORKLOAD_FLOOR_GIB",
    "RamHeadroom",
    "agent_workload_floor_gib",
    "cgroup_headroom_mib",
    "cgroup_v2_reclaimable_mib",
    "read_ram_headroom",
]
