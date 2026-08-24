"""Teatree-controlled disk/RAM pressure scanner — auto-free before OOM/full-disk (#128).

Mirrors :class:`SelfUpdateScanner`: a global (``overlay=""``) cadence-gated
scanner backed by a singleton :class:`ResourcePressureMarker`. It *measures*
absolute free resources every cadence window, classifies a pressure level,
and emits ``resource.*`` signals; a paired mechanical handler
(``free_resources``) does the actual freeing.

ABSOLUTE BYTES, NEVER PERCENT. Disk is measured as ``os.statvfs("/").f_bavail
* f_frsize`` (true free bytes) — a percent-of-nominal-total would misread an
APFS shared container (a 460 G nominal total with 7 G free reads as "69 %
full = fine" and the scanner never fires). RAM is read per platform: on Linux
``/proc/meminfo``'s ``MemAvailable``, the kernel's own estimate of what it can
hand out without swapping; on macOS the sum of the genuinely-reclaimable
``vm_stat`` page classes (free + inactive + purgeable + speculative), which
approximates the same quantity — macOS keeps RAM ~99 % "used" by design
(compressor + inactive cache), so a naive percent fires constantly and means
nothing. The machine figure is then floored by this process's own cgroup
headroom, because a capped container OOMs at its own ceiling while the host
still looks roomy.

A PROBE THAT CANNOT ANSWER SAYS SO (#4104). ``vm_stat`` is macOS-only, so the
single-reader version returned ``None`` on every Linux pass and the caller
silently skipped the whole RAM ladder — a scanner reporting "0 signals" on a
box with 2 GB of 30 available, indistinguishable from a healthy one. When no
scope can answer, the tick now emits ``resource.probe_inert`` instead of
nothing. An unreadable resource is still treated as *unbounded* free (never
0 GB) for the thresholds, so "can't tell" never fires a freeing pass either.

Decision ladder per tick. L0 OBSERVE — both resources above WARN and both
readable: measure + upsert marker, emit nothing (silent tick). L1 WARN — disk OR ram in the WARN
band: advisory ``resource.pressure_warn`` to the statusline, no freeing. L2
CRITICAL — disk OR ram below the CRIT threshold AND the freeing rate-limit has
elapsed: ``resource.cleanup_needed`` to the mechanical handler (allow-list
cache purge, docker reclaim, dormant-venv eviction, the proven-done worktree
sweep, idle-container stop — each losing nothing that is not rebuilt on demand).
L3 CRITICAL DESTRUCTIVE — flag-gated: the heuristic worktree GC
(``allow_destructive_disk``) and renderer SIGTERM (``allow_destructive_ram``
after >= 2 consecutive CRITICAL-RAM ticks) live in the handler, never run
without an explicit opt-in.

Every action is best-effort: a measurement or freeing failure logs and
returns rather than crashing the tick (mirrors ``SelfUpdateScanner``).
"""

import logging
import os
import platform
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal
from teatree.utils.ram_probe import linux_mem_available_kb
from teatree.utils.ram_scope import cgroup_headroom_mib
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker

logger = logging.getLogger(__name__)

_GIB = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ResourceReading:
    """One absolute-bytes snapshot of free disk + reclaimable RAM.

    ``None`` is "this host could not be asked", which is a different answer from
    a low number and must never be compared against a threshold as if it were one
    — the ``*_ladder_gb`` properties resolve it to unbounded free instead.
    """

    disk_free_gb: float | None
    ram_avail_gb: float | None

    @property
    def disk_ladder_gb(self) -> float:
        return self.disk_free_gb if self.disk_free_gb is not None else float("inf")

    @property
    def ram_ladder_gb(self) -> float:
        return self.ram_avail_gb if self.ram_avail_gb is not None else float("inf")

    @property
    def ram_probe_inert(self) -> bool:
        return self.ram_avail_gb is None


def read_disk_free_gb(path: str = "/") -> float | None:
    """Absolute free disk space in GB via ``os.statvfs`` (never percent).

    ``f_bavail`` is the blocks available to a non-privileged process;
    multiplied by ``f_frsize`` (fragment size) it is the true allocatable
    free-byte count, NOT a fraction of the (misleading on APFS) nominal
    container total. Returns ``None`` on any OS error so the caller treats
    the measurement as unavailable rather than crashing the tick.
    """
    try:
        stat = os.statvfs(path)
    except OSError:
        logger.warning("resource_pressure: os.statvfs(%r) failed", path)
        return None
    return (stat.f_bavail * stat.f_frsize) / _GIB


def read_ram_avail_gb() -> float | None:
    """Absolute reclaimable RAM in GB — the tightest scope that can answer.

    Two scopes can each run out independently, so the honest figure is whichever
    binds first: the machine (Linux ``/proc/meminfo`` ``MemAvailable``, macOS's
    reclaimable ``vm_stat`` page classes) and this process's own cgroup. Reading
    only the machine is how a 23 GB-capped worker was OOM-killed three times while
    the 30 GB host still reported ~10 GB free, an order of magnitude above every
    threshold (#4104). Mirrors :func:`ram_scope.read_ram_headroom`, which the sibling
    intake scanner in this same mini-loop already reasons in — and shares its cgroup
    reader outright, so the two cannot drift.

    ``None`` means no scope could answer — the caller reports the RAM ladder INERT
    rather than skipping it silently.
    """
    system = platform.system()
    readings = [gb for gb in (_read_machine_ram_avail_gb(system), _read_cgroup_headroom_gb()) if gb is not None]
    if not readings:
        logger.warning("resource_pressure: %s — the RAM ladder is inert", _ram_inert_reason(system))
        return None
    return min(readings)


def _read_machine_ram_avail_gb(system: str) -> float | None:
    """Machine-wide available RAM in GB, per platform."""
    if system == "Linux":
        available_kb = linux_mem_available_kb()
        return None if available_kb is None else available_kb * 1024 / _GIB
    if system == "Darwin":
        return _read_vm_stat_avail_gb()
    return None


def _read_cgroup_headroom_gb() -> float | None:
    """What this process's cgroup can still allocate — the scope a container OOMs in.

    Delegated to :func:`~teatree.utils.ram_scope.cgroup_headroom_mib` so this reader and
    the admission governor's cannot drift on what "available" means: both credit the
    reclaimable page cache ``memory.current`` charges, exactly as ``MemAvailable`` does
    on the machine arm above (#4217).
    """
    headroom_mib = cgroup_headroom_mib()
    return None if headroom_mib is None else headroom_mib / 1024


def _ram_inert_reason(system: str) -> str:
    """Why no reading was possible — an operator chasing a platform gap needs the real cause."""
    machine_reader = {"Linux": "/proc/meminfo unreadable", "Darwin": "vm_stat unavailable"}
    return f"{machine_reader.get(system, f'no RAM reader for platform {system}')} and no cgroup memory limit"


def _read_vm_stat_avail_gb() -> float | None:
    """Reclaimable macOS RAM in GB from ``vm_stat``, ``None`` when it cannot be read.

    The honest "available" figure on macOS is the sum of the page classes the
    OS can hand back without swapping: free + inactive + purgeable +
    speculative. Wired/active/compressed pages are genuinely committed.
    """
    vm_stat = shutil.which("vm_stat")
    if vm_stat is None:
        return None
    try:
        proc = run_allowed_to_fail([vm_stat], expected_codes=None, timeout=10)
    except (OSError, CommandFailedError):
        logger.warning("resource_pressure: vm_stat invocation failed")
        return None
    if proc.returncode != 0:
        return None
    return _parse_vm_stat_avail_gb(proc.stdout)


def _parse_vm_stat_avail_gb(output: str) -> float | None:
    """Sum the reclaimable ``vm_stat`` page classes into GB, ``None`` if unparsable."""
    page_size = _vm_stat_page_size(output)
    if page_size is None:
        return None
    reclaimable_labels = (
        "Pages free",
        "Pages inactive",
        "Pages purgeable",
        "Pages speculative",
    )
    total_pages = 0
    found_any = False
    for label in reclaimable_labels:
        pages = _vm_stat_pages_for(output, label)
        if pages is not None:
            total_pages += pages
            found_any = True
    if not found_any:
        return None
    return (total_pages * page_size) / _GIB


def _vm_stat_page_size(output: str) -> int | None:
    """Extract the page size (bytes) from the ``vm_stat`` header line."""
    for line in output.splitlines():
        if "page size of" in line:
            for token in line.replace(")", "").split():
                if token.isdigit():
                    return int(token)
    return None


def _vm_stat_pages_for(output: str, label: str) -> int | None:
    """Return the page count for *label* (e.g. ``"Pages free"``), ``None`` if absent."""
    for line in output.splitlines():
        if line.strip().startswith(label):
            digits = line.split(":", 1)[-1].strip().rstrip(".").replace(",", "")
            if digits.isdigit():
                return int(digits)
    return None


def _measure() -> ResourceReading:
    """Read both resources, recording an unmeasurable one as ``None``."""
    return ResourceReading(disk_free_gb=read_disk_free_gb(), ram_avail_gb=read_ram_avail_gb())


def _ram_probe_inert_signal() -> ScanSignal:
    """Say the RAM ladder is not protecting this host, so it reads differently from a healthy one."""
    system = platform.system()
    reason = _ram_inert_reason(system)
    return ScanSignal(
        kind="resource.probe_inert",
        summary=f"ram probe INERT on {system} — {reason}; the RAM ladder is not guarding this host",
        payload={"resource": "ram", "level": "inert", "platform": system, "reason": reason},
    )


def _track_consecutive_critical(*, marker: "ResourcePressureMarker", ram_crit: bool) -> None:
    """Increment (on CRITICAL RAM) or reset the sustained-CRITICAL counter."""
    current = getattr(marker, "consecutive_critical", 0) or 0
    new_value = current + 1 if ram_crit else 0
    if new_value == current:
        return
    try:
        marker.consecutive_critical = new_value
        marker.save(update_fields=["consecutive_critical"])
    except Exception:
        logger.exception("resource_pressure: failed to update consecutive_critical")


@dataclass(slots=True)
class ResourcePressureScanner:
    """Measure free disk + reclaimable RAM and emit ``resource.*`` signals.

    Threshold fields are absolute GB. ``cadence_minutes`` gates how often the
    scanner re-measures (decoupled from the loop tick cadence so a sub-minute
    tick doesn't re-shell ``vm_stat``). ``min_free_interval_minutes`` is the
    anti-thrash gap between *freeing* passes — measurement still runs every
    cadence window, only ``resource.cleanup_needed`` is rate-limited. All
    destructive levers default OFF and live in the mechanical handler; the
    scanner only emits the signal that *invites* freeing, plus the allow-list
    + flag payload the handler consults.
    """

    disk_warn_free_gb: float = 25.0
    disk_crit_free_gb: float = 10.0
    ram_warn_avail_gb: float = 3.0
    ram_crit_avail_gb: float = 1.5
    cadence_minutes: int = 5
    min_free_interval_minutes: int = 30
    disk_cache_allowlist: tuple[str, ...] = ()
    allow_destructive_disk: bool = False
    venv_idle_days: float = 2.0
    worktree_stale_days: int = 30
    max_worktree_gc_per_tick: int = 3
    allow_destructive_ram: bool = False
    ram_kill_allowlist: tuple[str, ...] = field(default_factory=tuple)
    scratch_retention_days: int = 0
    scratch_sweep_root: str = ""
    name: str = "resource_pressure"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.models.resource_pressure_marker import ResourcePressureMarker  # noqa: PLC0415 — lazy ORM

        try:
            marker = ResourcePressureMarker.load()
        except Exception:
            logger.exception("resource_pressure: could not load marker — skipping tick")
            return []
        if self._cadence_blocks(marker):
            return []
        reading = _measure()
        try:
            marker.record_measurement(
                disk_free_gb=reading.disk_free_gb,
                ram_avail_gb=reading.ram_avail_gb,
            )
        except Exception:
            logger.exception("resource_pressure: failed to persist measurement")
        return self._classify(reading=reading, marker=marker)

    def _cadence_blocks(self, marker: object) -> bool:
        """True iff the measurement cadence has not yet elapsed since ``last_run_at``."""
        last_run = getattr(marker, "last_run_at", None)
        if last_run is None:
            return False
        elapsed_minutes = (timezone.now() - last_run).total_seconds() / 60.0
        return elapsed_minutes < self.cadence_minutes

    def _classify(self, *, reading: ResourceReading, marker: "ResourcePressureMarker") -> list[ScanSignal]:
        inert = [_ram_probe_inert_signal()] if reading.ram_probe_inert else []
        disk_crit = reading.disk_ladder_gb < self.disk_crit_free_gb
        ram_crit = reading.ram_ladder_gb < self.ram_crit_avail_gb
        _track_consecutive_critical(marker=marker, ram_crit=ram_crit)
        if disk_crit or ram_crit:
            return inert + self._critical_signals(
                reading=reading,
                marker=marker,
                disk_crit=disk_crit,
                ram_crit=ram_crit,
            )
        disk_warn = reading.disk_ladder_gb < self.disk_warn_free_gb
        ram_warn = reading.ram_ladder_gb < self.ram_warn_avail_gb
        if disk_warn or ram_warn:
            return inert + self._warn_signals(reading=reading, disk_warn=disk_warn, ram_warn=ram_warn)
        return inert

    def _warn_signals(self, *, reading: ResourceReading, disk_warn: bool, ram_warn: bool) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        if disk_warn:
            signals.append(
                ScanSignal(
                    kind="resource.pressure_warn",
                    summary=f"disk {reading.disk_ladder_gb:.1f} GB free (warn < {self.disk_warn_free_gb:.0f} GB)",
                    payload={"resource": "disk", "free_gb": reading.disk_ladder_gb, "level": "warn"},
                ),
            )
        if ram_warn:
            signals.append(
                ScanSignal(
                    kind="resource.pressure_warn",
                    summary=f"ram {reading.ram_ladder_gb:.1f} GB avail (warn < {self.ram_warn_avail_gb:.0f} GB)",
                    payload={"resource": "ram", "avail_gb": reading.ram_ladder_gb, "level": "warn"},
                ),
            )
        return signals

    def _critical_signals(
        self,
        *,
        reading: ResourceReading,
        marker: object,
        disk_crit: bool,
        ram_crit: bool,
    ) -> list[ScanSignal]:
        if self._free_rate_limited(marker):
            # Surface the CRITICAL band even when the freeing pass is throttled,
            # so the user sees the pressure without a second purge being kicked off.
            return self._warn_signals(
                reading=reading,
                disk_warn=disk_crit,
                ram_warn=ram_crit,
            )
        signals: list[ScanSignal] = []
        if disk_crit:
            signals.append(self._cleanup_needed_signal(resource="disk", reading=reading, marker=marker))
        if ram_crit:
            signals.append(self._cleanup_needed_signal(resource="ram", reading=reading, marker=marker))
        return signals

    def _free_rate_limited(self, marker: object) -> bool:
        """True iff a freeing pass ran within ``min_free_interval_minutes`` (anti-thrash)."""
        last_freed = getattr(marker, "last_freed_at", None)
        if last_freed is None:
            return False
        elapsed_minutes = (timezone.now() - last_freed).total_seconds() / 60.0
        return elapsed_minutes < self.min_free_interval_minutes

    def _cleanup_needed_signal(self, *, resource: str, reading: ResourceReading, marker: object) -> ScanSignal:
        free_gb = reading.disk_ladder_gb if resource == "disk" else reading.ram_ladder_gb
        crit_gb = self.disk_crit_free_gb if resource == "disk" else self.ram_crit_avail_gb
        return ScanSignal(
            kind="resource.cleanup_needed",
            summary=f"{resource} CRITICAL: {free_gb:.1f} GB (< {crit_gb:.1f} GB) — freeing",
            payload={
                "resource": resource,
                "free_gb": free_gb,
                "level": "critical",
                "disk_cache_allowlist": list(self.disk_cache_allowlist),
                "allow_destructive_disk": self.allow_destructive_disk,
                "venv_idle_days": self.venv_idle_days,
                "worktree_stale_days": self.worktree_stale_days,
                "max_worktree_gc_per_tick": self.max_worktree_gc_per_tick,
                "allow_destructive_ram": self.allow_destructive_ram,
                "ram_kill_allowlist": list(self.ram_kill_allowlist),
                "scratch_retention_days": self.scratch_retention_days,
                "scratch_sweep_root": self.scratch_sweep_root,
                "consecutive_critical": getattr(marker, "consecutive_critical", 0) or 0,
            },
        )


__all__ = [
    "ResourcePressureScanner",
    "ResourceReading",
    "read_disk_free_gb",
    "read_ram_avail_gb",
]
