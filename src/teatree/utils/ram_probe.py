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
    import shutil  # noqa: PLC0415

    from teatree.utils.run import CommandFailedError, run_checked  # noqa: PLC0415

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
        with open("/proc/meminfo", encoding="utf-8") as handle:  # noqa: PTH123
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


def default_provision_concurrency(cpu_count: int | None = None) -> int:
    """nCPU-derived default concurrency cap for parallel worktree provisioning.

    Each worktree's provision subprocess is I/O-heavy (network, DB, docker)
    but still spends real CPU time (Django boot, ``migrate``, ``uv sync``).
    Half the host's logical cores — floored at 1 — keeps enough headroom for
    the RAM-based admission gate to still matter on a cold multi-repo
    provision instead of every core being saturated the instant it fires.
    """
    n = cpu_count if cpu_count is not None else (os.cpu_count() or 2)
    return max(1, n // 2)


__all__ = ["default_provision_concurrency", "read_ram_used_percent"]
