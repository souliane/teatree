"""Box memory evidence captured when a pressure repair ticket opens."""

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from teatree.config import discover_overlays, worktree_root
from teatree.core.cleanup.orphan_process_groups import CLOCK_TICKS_PER_SECOND, ProcStat, collect_stats, read_uptime
from teatree.core.cleanup.process_table import current_pid_namespace, host_proc_root, namespace_pids, pid_namespace_of
from teatree.core.loop_lease_liveness import namespace_is_proven
from teatree.loop.self_improve.detectors.base import DetectorReport
from teatree.loop.self_improve.detectors.pressure_incident import PressureIncidentDetector
from teatree.utils import ram_scope, singleton
from teatree.utils.singleton import WORKER_SINGLETON, default_pid_path

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
_TOP_PROCESSES = 10
_STAT_KEYS = ("anon", "shmem", "active_file", "inactive_file", "slab_reclaimable")
_AGENT_COMM = re.compile(r"(?:claude|codex|\d+\.\d+\.\d+)", re.IGNORECASE)
_NO_PROCESS_TABLE = "Top processes by RSS: scan unavailable (no process table)"


def _unreadable(exc: Exception) -> str:
    return f"unreadable ({type(exc).__name__})"


def _safe_label(value: str) -> str:
    return re.sub(r"[^\w .-]", "_", value)[:80]


def _reading(value: int | None, trace: dict[str, int | str], *keys: str) -> str:
    if value is not None:
        return f"{value} MiB"
    for key in keys:
        observed = trace.get(key)
        if observed == "no cap":
            return "no cap"
        if isinstance(observed, str):
            return f"unreadable ({observed})"
    return "unavailable"


def _memory_stat_line() -> str:
    try:
        stat = ram_scope.read_cgroup_memory_stat()
    except OSError as exc:
        return f"memory.stat: {_unreadable(exc)}"
    parts = [f"{key}={stat[key] // _MIB} MiB" if key in stat else f"{key}=unavailable" for key in _STAT_KEYS]
    return f"memory.stat: {', '.join(parts)}"


def _memory_lines() -> list[str]:
    trace: dict[str, int | str] = {}
    headroom = ram_scope.read_ram_headroom(trace=trace)
    host = headroom.host_available_mib
    if host is None and isinstance(meminfo := trace.get("meminfo"), int):
        host = meminfo
    return [
        f"Judged RAM: {_reading(headroom.box_watermark_mib, trace)} ({headroom.binding_scope} bound)",
        f"Host MemAvailable: {_reading(host, trace, 'meminfo')}",
        (
            f"Cgroup cap: {_reading(headroom.cgroup_limit_mib, trace, 'memory.max')}; "
            f"headroom: {_reading(headroom.cgroup_headroom_mib, trace, 'memory.max', 'memory.current')}; "
            f"box-scoped: {'yes' if headroom.cgroup_is_box_scoped else 'no'}"
        ),
        _memory_stat_line(),
    ]


def _ticket_roots() -> tuple[Path, ...]:
    return tuple(dict.fromkeys(worktree_root(overlay=overlay.name) for overlay in discover_overlays()))


def _ticket_dir(pid_dir: Path, roots: tuple[Path, ...]) -> str:
    try:
        cwd = pid_dir.joinpath("cwd").readlink()
    except OSError as exc:
        return _unreadable(exc)
    for root in roots:
        try:
            relative = cwd.relative_to(root)
        except ValueError:
            continue
        if relative.parts:
            return _safe_label(relative.parts[0])
    return "-"


def _resident_pages(pid_dir: Path) -> int | None:
    try:
        fields = pid_dir.joinpath("statm").read_text(encoding="utf-8").split()
        return int(fields[1])
    except (OSError, ValueError, IndexError):
        return None


def _live_worker_record() -> tuple[singleton.HolderRecord | None, str | None]:
    errors: list[Exception] = []
    record = singleton.read_holder(default_pid_path(WORKER_SINGLETON), errors=errors)
    if errors:
        return None, _unreadable(errors[0])
    if record is not None and not namespace_is_proven(record.context.pid_namespace):
        return None, None
    try:
        alive = record is not None and singleton.pid_alive(record.pid)
    except OverflowError as exc:
        return None, _unreadable(exc)
    return (record if alive else None), None


def _worker_host_pid(root: Path, stats: dict[int, ProcStat]) -> tuple[int | None, str | None]:
    record, error = _live_worker_record()
    if record is None:
        return None, error
    own_namespace = current_pid_namespace()
    for pid in stats:
        pid_dir = root / str(pid)
        if namespace_pids(pid_dir)[-1:] == (str(record.pid),) and pid_namespace_of(pid_dir) == own_namespace:
            return pid, None
    return None, None


def _attached(pid: int, stats: dict[int, ProcStat], worker_pid: int | None) -> bool:
    seen: set[int] = set()
    while pid in stats and pid not in seen:
        seen.add(pid)
        stat = stats[pid]
        if pid == worker_pid or _AGENT_COMM.fullmatch(stat.comm):
            return True
        pid = stat.ppid
    return False


def _process_lines() -> list[str]:
    root, _reason = host_proc_root()
    if root is None or (uptime := read_uptime(root)) is None:
        return [_NO_PROCESS_TABLE]
    listed, _opaque = collect_stats(root, exclude_nested_namespaces=False)
    if not listed:
        return [_NO_PROCESS_TABLE]
    stats = {stat.pid: stat for stat in listed}
    pages_by_pid = {pid: pages for pid in stats if (pages := _resident_pages(root / str(pid))) is not None}
    top = sorted(pages_by_pid, key=lambda pid: (-pages_by_pid[pid], pid))[:_TOP_PROCESSES]
    page_size = os.sysconf("SC_PAGE_SIZE")
    roots = _ticket_roots()
    worker_pid, worker_error = _worker_host_pid(root, stats)
    lines = [f"Top processes by RSS (up to {_TOP_PROCESSES}; attached = ancestry reaches the worker or an agent CLI):"]
    if worker_error:
        lines.append(f"Worker attribution: {worker_error}")
    for pid in top:
        stat = stats[pid]
        age = max(0, int(uptime - stat.start_ticks / CLOCK_TICKS_PER_SECOND))
        lines.append(
            f"pid={pid} ppid={stat.ppid} age={age}s rss={pages_by_pid[pid] * page_size // _MIB} MiB "
            f"comm={_safe_label(stat.comm)} ticket-dir={_ticket_dir(root / str(pid), roots)} "
            f"{'attached' if _attached(pid, stats, worker_pid) else 'detached'}"
        )
    return lines


def box_memory_snapshot(report: DetectorReport) -> str:
    if report.detector != PressureIncidentDetector.name or report.payload.get("cause") not in {"memory", "swap"}:
        return ""
    lines = [f"Box memory snapshot ({datetime.now(UTC).isoformat(timespec='seconds')}):"]
    for heading, read in (("Memory scopes", _memory_lines), ("Top processes by RSS", _process_lines)):
        try:
            lines.extend(read())
        except Exception as exc:
            logger.warning("Box memory snapshot: %s could not be read", heading, exc_info=True)
            lines.append(f"{heading}: {_unreadable(exc)}")
    return "\n".join(lines)
