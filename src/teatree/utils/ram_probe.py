"""Host RAM-utilisation probe, shared by the self-improve budget gate and the provisioning admission gate.

Mirrors the same probe shape ``hooks/scripts/statusline.sh`` uses (``sysctl``
on macOS, ``/proc/meminfo`` on Linux). Dependency-free so a missing optional
library never crashes a caller; each platform read degrades to ``0.0`` on any
subprocess/parse failure rather than raising, since "can't tell" must never
look like "under pressure" to a caller gating on a high-usage threshold.
"""

import os
import platform


def _macos_ram_used_percent() -> float:
    """Read mac RAM use via ``sysctl`` + ``vm_stat`` (mirrors statusline.sh)."""
    import shutil  # noqa: PLC0415 — deferred: loaded only on this code path

    from teatree.utils.run import CommandFailedError, run_checked  # noqa: PLC0415 — deferred: call-time import

    sysctl = shutil.which("sysctl")
    vm_stat = shutil.which("vm_stat")
    if not sysctl or not vm_stat:
        return 0.0
    try:
        total = int(run_checked([sysctl, "-n", "hw.memsize"], timeout=2).stdout.strip())
        if total <= 0:
            return 0.0
        stat = run_checked([vm_stat], timeout=2).stdout
    except (CommandFailedError, ValueError, OSError, TimeoutError):
        return 0.0
    page_size, free_pages, inactive_pages = 4096, 0, 0
    for line in stat.splitlines():
        if line.startswith("Pages free:"):
            free_pages = int(line.split(":")[1].strip().rstrip("."))
        elif line.startswith("Pages inactive:"):
            inactive_pages = int(line.split(":")[1].strip().rstrip("."))
    used = total - (free_pages + inactive_pages) * page_size
    return max(0.0, min(100.0, used * 100.0 / total))


def _linux_ram_used_percent() -> float:
    """Read Linux RAM use via ``/proc/meminfo`` (mirrors statusline.sh)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:  # noqa: PTH123 — builtin open on a device/proc path; Path.open adds nothing here
            lines = handle.readlines()
    except OSError:
        return 0.0
    info: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        value = rest.strip().split(" ", 1)[0]
        if value.isdigit():
            info[key.strip()] = int(value)
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    if total <= 0:
        return 0.0
    used = total - avail
    return max(0.0, min(100.0, used * 100.0 / total))


def read_ram_used_percent() -> float:
    """Best-effort read of system RAM utilisation percent (0-100).

    Callers inject a known value in tests via their own seam (e.g.
    ``precheck_budget(ram_used_percent=...)``, ``check_provision_admission(ram_used_percent=...)``)
    — this function is only consulted when no explicit sample is provided.
    """
    system = platform.system()
    if system == "Darwin":
        return _macos_ram_used_percent()
    if system == "Linux":
        return _linux_ram_used_percent()
    return 0.0


def _cgroup_v2_cpu_quota() -> int | None:
    """Cores permitted by the cgroup-v2 CPU quota, or ``None`` when unlimited/absent.

    ``/sys/fs/cgroup/cpu.max`` holds ``"<quota> <period>"`` (or ``"max <period>"``
    when uncapped). A capped container reports a quota well below the host's
    physical cores, so honouring it keeps :func:`available_cpu_count` from
    reading the host's 8 cores inside a 2-core-capped container (#3409). Rounds
    the quota/period ratio up and floors at 1 — a fractional sub-core quota still
    admits one worker. Any read/parse failure degrades to ``None`` (treated as
    "no cgroup cap"), never raising.
    """
    from pathlib import Path  # noqa: PLC0415 — deferred: loaded only on this code path

    try:
        quota_raw, _, period_raw = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="utf-8").strip().partition(" ")
        if quota_raw == "max":
            return None
        quota, period = int(quota_raw), int(period_raw)
    except (OSError, ValueError):
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, -(-quota // period))  # ceil division, floored at 1


def available_cpu_count() -> int:
    """Cores actually available to THIS process — cgroup/affinity-aware (#3409).

    A container capped below the host must not derive its concurrency from the
    host's physical core count. The minimum of every signal we can read is the
    honest ceiling: CPU-affinity (``os.process_cpu_count`` on 3.13, else
    ``sched_getaffinity``), the physical ``os.cpu_count``, and the cgroup-v2 CPU
    quota. Floored at 1 so a caller always gets a usable positive count.
    """
    candidates: list[int] = []
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        affinity = process_cpu_count()
        if affinity:
            candidates.append(affinity)
    elif hasattr(os, "sched_getaffinity"):
        candidates.append(len(os.sched_getaffinity(0)))
    physical = os.cpu_count()
    if physical:
        candidates.append(physical)
    quota = _cgroup_v2_cpu_quota()
    if quota is not None:
        candidates.append(quota)
    return max(1, min(candidates)) if candidates else 1


def default_provision_concurrency(cpu_count: int | None = None) -> int:
    """nCPU-derived default concurrency cap for parallel worktree provisioning.

    Each worktree's provision subprocess is I/O-heavy (network, DB, docker)
    but still spends real CPU time (Django boot, ``migrate``, ``uv sync``).
    Half the process's *available* logical cores — floored at 1 — keeps enough
    headroom for the RAM-based admission gate to still matter on a cold
    multi-repo provision instead of every core being saturated the instant it
    fires. The available-core read is cgroup/affinity-aware
    (:func:`available_cpu_count`) so a capped container derives from its cap,
    not the host (#3409).
    """
    n = cpu_count if cpu_count is not None else available_cpu_count()
    return max(1, n // 2)


__all__ = ["available_cpu_count", "default_provision_concurrency", "read_ram_used_percent"]
