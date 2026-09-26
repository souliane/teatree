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

A PROBE THAT CANNOT ANSWER SAYS SO (#4104), ON BOTH RESOURCES. ``vm_stat`` is
macOS-only, so the single-reader version returned ``None`` on every Linux pass
and the caller silently skipped the whole RAM ladder — a scanner reporting
"0 signals" on a box with 2 GB of 30 available, indistinguishable from a healthy
one. Disk then reacquired the same shape when the probe path moved off ``/``:
``worktree_root()`` is a pure resolver that never touches the filesystem, so it
routinely names a path that does not exist, ``statvfs`` raised, and the reading
degraded to ``None`` -> ``inf`` -> healthy with no signal at all. Both halves now
fall back where a fallback exists (disk walks the worktree root's ANCESTORS, which sit
on the volume that was meant, before ``/``) and emit ``resource.probe_inert`` when nothing
can answer. A reading that had to come from ``/`` after all emits
``resource.probe_degraded``: it is a real number about the wrong volume, which is the
container-rootfs misread this scanner exists to remove. An unreadable resource is still
treated as *unbounded* free (never 0 GB) for the thresholds, so "can't tell"
never fires a freeing pass either — but it is never SILENT about it.

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
from pathlib import Path
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.config import worktree_root
from teatree.loop.scanners.base import ScanSignal
from teatree.utils.ram_probe import linux_mem_available_kb
from teatree.utils.ram_scope import cgroup_headroom_mib, cgroup_memory_probe_inert
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
    #: Every path the disk probe tried, in order, and the one that answered — carried so a
    #: signal reports what was ACTUALLY probed instead of re-deriving a ladder that may
    #: have moved since.
    disk_probed_paths: tuple[str, ...] = ()
    disk_answered_by: str = ""
    cgroup_probe_inert: bool = False

    @property
    def disk_ladder_gb(self) -> float:
        return self.disk_free_gb if self.disk_free_gb is not None else float("inf")

    @property
    def ram_ladder_gb(self) -> float:
        return self.ram_avail_gb if self.ram_avail_gb is not None else float("inf")

    @property
    def disk_probe_inert(self) -> bool:
        return self.disk_free_gb is None

    @property
    def disk_probe_degraded(self) -> bool:
        """True iff only the filesystem root answered, on a host that meant a different volume.

        The reading is then about the container's own rootfs rather than the volume the
        checkouts fill — the 384.0-vs-42.3 GB misread this scanner exists to remove. It is
        a real number, so it is used; but it is not the number that was asked for.
        """
        return self.disk_free_gb is not None and self.disk_answered_by == "/" and self.disk_probed_paths[:1] != ("/",)

    @property
    def ram_probe_inert(self) -> bool:
        return self.ram_avail_gb is None


def read_disk_free_gb(path: str) -> float | None:
    """Absolute free disk space in GB via ``os.statvfs`` (never percent).

    ``f_bavail`` is the blocks available to a non-privileged process;
    multiplied by ``f_frsize`` (fragment size) it is the true allocatable
    free-byte count, NOT a fraction of the (misleading on APFS) nominal
    container total. Returns ``None`` on any OS error so the caller treats
    the measurement as unavailable rather than crashing the tick.

    THE PATH IS NOT ``/`` FOR A REASON (#4244), which is why it has no default. The
    loop runs inside a container whose ``/`` is the Docker VM's own virtual disk, not
    the host volume the checkout pool fills. Measured in both venues: the container
    read 384.0 GB free on a host that had 42.3 GB, so the ladder classified a nearly-full
    box as healthy and NO lever on it could ever fire. :func:`_probe_disk_free_gb`
    therefore probes the worktree root, which is a host-backed bind mount there. A
    ``"/"`` default would let the next caller re-acquire that bug in one keystroke, so
    the parameter is REQUIRED — the caller states which volume it means.
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


def measure_resources() -> ResourceReading:
    """Read both resources, recording an unmeasurable one as ``None``."""
    paths = _disk_probe_paths()
    free_gb, answered_by = _probe_disk_free_gb(paths)
    return ResourceReading(
        disk_free_gb=free_gb,
        ram_avail_gb=read_ram_avail_gb(),
        disk_probed_paths=paths,
        disk_answered_by=answered_by,
        cgroup_probe_inert=cgroup_memory_probe_inert(),
    )


def _probe_disk_free_gb(paths: tuple[str, ...]) -> tuple[float | None, str]:
    """The first of *paths* that answers, and which one did — ``(None, "")`` when none does."""
    for path in paths:
        if (free_gb := read_disk_free_gb(path)) is not None:
            return free_gb, path
    return None, ""


def _disk_probe_paths() -> tuple[str, ...]:
    """The volume whose exhaustion is the incident, then each ancestor of it, ending at ``/``.

    Derived, never configured: the worktree root already names where the checkouts live,
    so a ``disk_probe_path`` setting would be surface paid at every call site to express a
    value the code knows. A venue that cannot resolve it (a pre-Django caller, no
    registered overlay) probes ``/`` alone rather than losing the reading.

    Walking the ANCESTORS is what keeps the fallback on the right volume.
    :func:`teatree.config.worktree_root` is a pure resolver that never touches the
    filesystem, so it routinely names a path nothing has created yet — the half that
    actually happens — and a ladder of ``(root, "/")`` then answers from ``/``, which
    inside the worker container is the Docker VM's own disk (measured: 384.0 GB free while
    the host volume had 42.3). Its parents — ``~/workspace/t3-workspaces``, then ``~`` —
    exist and sit on the volume that was meant, in both venues. ``/`` stays last so a
    reading is never lost, and reaching it marks the reading degraded.
    """
    try:
        root = Path(worktree_root())
    except Exception:
        logger.exception("resource_pressure: worktree root unresolvable — measuring / instead")
        return ("/",)
    return tuple(dict.fromkeys([*(str(path) for path in (root, *root.parents)), "/"]))


def _inert_signals(reading: ResourceReading) -> list[ScanSignal]:
    """One signal per probe that could not answer, or answered about the wrong volume."""
    signals: list[ScanSignal] = []
    if reading.disk_probe_inert:
        signals.append(_disk_probe_inert_signal(reading))
    elif reading.disk_probe_degraded:
        signals.append(_disk_probe_degraded_signal(reading))
    if reading.ram_probe_inert:
        signals.append(_ram_probe_inert_signal())
    if reading.cgroup_probe_inert:
        signals.append(
            ScanSignal(
                kind="resource.probe_inert",
                summary="cgroup RAM probe INERT — worker memory cap/usage unreadable; host RAM may hide OOM risk",
                payload={
                    "resource": "cgroup",
                    "level": "inert",
                    "reason": "memory cap or usage unreadable (an explicit unlimited cap is not inert)",
                },
            )
        )
    return signals


def _disk_probe_inert_signal(reading: ResourceReading) -> ScanSignal:
    """Say the DISK ladder is not guarding this host — the loud half the disk arm lacked.

    RAM has said so since #4104; disk said nothing. ``statvfs`` failing logged a warning
    nobody reads, the reading resolved to unbounded free, and a full box was
    indistinguishable from a healthy one — the same silent-inert failure #4104 named,
    on the other resource. A gate that cannot measure fails LOUD, never crash-open.

    The probed paths come from the READING rather than a second ``_disk_probe_paths()``
    call, which would log its own exception again and can name a different ladder.
    """
    reason = f"os.statvfs answered for none of {', '.join(reading.disk_probed_paths)}"
    return ScanSignal(
        kind="resource.probe_inert",
        summary=f"disk probe INERT — {reason}; the disk ladder is not guarding this host",
        payload={"resource": "disk", "level": "inert", "reason": reason},
    )


def _disk_probe_degraded_signal(reading: ResourceReading) -> ScanSignal:
    """Say the reading came from ``/`` rather than the volume that was asked about.

    A degraded reading is confidently wrong rather than absent: inside the worker container
    ``/`` is the Docker VM's own disk, so it reports roomy while the host volume fills. The
    ladder still uses the number — it is better than none — but it says so, because the
    remedy (create the worktree root, or fix the bind mount) is the operator's.
    """
    intended = reading.disk_probed_paths[0] if reading.disk_probed_paths else "the worktree root"
    reason = f"nothing under {intended} could be statted, so the reading is the filesystem root's"
    return ScanSignal(
        kind="resource.probe_degraded",
        summary=f"disk probe DEGRADED — {reason}; the disk ladder may be guarding the wrong volume",
        payload={
            "resource": "disk",
            "level": "degraded",
            "reason": reason,
            "free_gb": reading.disk_ladder_gb,
            "answered_by": reading.disk_answered_by,
        },
    )


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
    worktree_stale_days: int = 30
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
        reading = measure_resources()
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
        inert = _inert_signals(reading)
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
                # The stall signal reads how far below these the box actually is (#4644),
                # so it needs the shape of the ladder it was dispatched from, not just
                # the reading.
                "disk_warn_free_gb": self.disk_warn_free_gb,
                "disk_crit_free_gb": self.disk_crit_free_gb,
                "worktree_stale_days": self.worktree_stale_days,
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
