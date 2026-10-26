"""Box memory evidence captured when a pressure repair ticket opens."""

import os
import re
from datetime import UTC, datetime
from pathlib import Path

from teatree.config import discover_overlays, worktree_root
from teatree.config.checkout_roots import checkout_roots
from teatree.core.cleanup.orphan_process_groups import CLOCK_TICKS_PER_SECOND, ProcStat, read_stat, read_uptime
from teatree.core.cleanup.process_table import current_pid_namespace, host_proc_root, namespace_pids, pid_namespace_of
from teatree.loop.self_improve.detectors.base import DetectorReport
from teatree.utils import ram_probe, ram_scope
from teatree.utils.singleton import WORKER_SINGLETON, default_pid_path, read_pid

_MIB = 1024 * 1024
_TOP_PROCESSES = 10
_AGENT_COMM = re.compile(r"(?:claude|codex|\d+\.\d+\.\d+)", re.IGNORECASE)


def _unreadable(exc: Exception) -> str:
    return f"unreadable ({type(exc).__name__})"


def _safe_label(value: str) -> str:
    return re.sub(r"[^\w .-]", "_", value)[:80]


def _reading(value: int | None, filename: str, *, no_cap: bool = False) -> str:
    if value is not None:
        return f"{value} MiB"
    try:
        if filename == "meminfo":
            ram_probe.RamProbePaths.meminfo().read_text(encoding="utf-8")
        else:
            raw = ram_scope.cgroup_file(filename).read_text(encoding="utf-8").strip()
            if no_cap and raw == "max":
                return "no cap"
    except OSError as exc:
        return _unreadable(exc)
    return "unreadable (ValueError)"


def _memory_lines() -> list[str]:
    try:
        headroom = ram_scope.read_ram_headroom()
    except OSError as exc:
        return [f"Memory scopes: {_unreadable(exc)}"]
    judged = _reading(headroom.box_watermark_mib, "meminfo")
    cap = _reading(headroom.cgroup_limit_mib, "memory.max", no_cap=True)
    cgroup_headroom = "no cap" if cap == "no cap" else _reading(headroom.cgroup_headroom_mib, "memory.current")
    lines = [
        f"Judged RAM: {judged} ({headroom.binding_scope} bound)",
        f"Host MemAvailable: {_reading(headroom.host_available_mib, 'meminfo')}",
        (
            f"Cgroup cap: {cap}; "
            f"headroom: {cgroup_headroom}; "
            f"box-scoped: {'yes' if headroom.cgroup_is_box_scoped else 'no'}"
        ),
    ]
    try:
        stat = ram_scope.read_cgroup_memory_stat()
    except OSError as exc:
        lines.append(f"memory.stat: {_unreadable(exc)}")
    else:
        parts = [
            f"{key}={stat[key] // _MIB} MiB" if key in stat else f"{key}=unreadable (KeyError)"
            for key in ("anon", "shmem", "active_file", "inactive_file", "slab_reclaimable")
        ]
        lines.append(f"memory.stat: {', '.join(parts)}")
    return lines


def _ticket_roots() -> tuple[Path, ...]:
    roots = checkout_roots()
    candidates = [*roots.durable, *roots.disposable, *roots.harness]
    candidates.extend(worktree_root(overlay=overlay.name) for overlay in discover_overlays())
    return tuple(sorted(set(candidates), key=lambda root: len(root.parts), reverse=True))


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


def _worker_host_pid(root: Path, stats: dict[int, ProcStat]) -> int | None:
    worker_pid = read_pid(default_pid_path(WORKER_SINGLETON))
    if worker_pid is None:
        return None
    own_namespace = current_pid_namespace()
    for pid in stats:
        pid_dir = root / str(pid)
        if namespace_pids(pid_dir)[-1:] != (str(worker_pid),):
            continue
        if own_namespace and pid_namespace_of(pid_dir) == own_namespace:
            return pid
    return worker_pid if root == Path("/proc") and worker_pid in stats else None


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
        return ["Top processes by RSS: scan unavailable (no process table)"]
    try:
        pid_dirs = (entry for entry in root.iterdir() if entry.name.isdigit())
        page_size = os.sysconf("SC_PAGE_SIZE")
        ranked: list[tuple[int, ProcStat]] = []
        stats: dict[int, ProcStat] = {}
        for pid_dir in pid_dirs:
            stat = read_stat(pid_dir)
            if stat is None:
                continue
            stats[stat.pid] = stat
            if (pages := _resident_pages(pid_dir)) is not None:
                ranked.append((pages, stat))
    except OSError:
        return ["Top processes by RSS: scan unavailable (no process table)"]
    ranked.sort(key=lambda row: (-row[0], row[1].pid))
    roots = _ticket_roots()
    worker_pid = _worker_host_pid(root, stats)
    lines = [f"Top processes by RSS (up to {_TOP_PROCESSES}):"]
    for pages, stat in ranked[:_TOP_PROCESSES]:
        age = max(0, int(uptime - stat.start_ticks / CLOCK_TICKS_PER_SECOND))
        comm = _safe_label(stat.comm)
        lines.append(
            f"pid={stat.pid} ppid={stat.ppid} age={age}s rss={pages * page_size // _MIB} MiB "
            f"comm={comm} ticket-dir={_ticket_dir(root / str(stat.pid), roots)} "
            f"{'attached' if _attached(stat.pid, stats, worker_pid) else 'detached'}"
        )
    return lines


def box_memory_snapshot(report: DetectorReport) -> str:
    if report.detector != "pressure_incident" or report.payload.get("cause") not in {"memory", "swap"}:
        return ""
    lines = [f"Box memory snapshot ({datetime.now(UTC).isoformat(timespec='seconds')}):"]
    lines.extend(_memory_lines())
    lines.extend(_process_lines())
    return "\n".join(lines)
