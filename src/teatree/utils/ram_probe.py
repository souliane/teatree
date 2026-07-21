"""Host RAM-utilisation probe, shared by the self-improve budget gate and the provisioning admission gate.

Mirrors the same probe shape ``hooks/scripts/statusline.sh`` uses (``sysctl``
on macOS, ``/proc/meminfo`` on Linux). Dependency-free so a missing optional
library never crashes a caller; each platform read degrades to ``0.0`` on any
subprocess/parse failure rather than raising, since "can't tell" must never
look like "under pressure" to a caller gating on a high-usage threshold.

Doubles as the deploy-time worker-sizing helper (#3432): run from the HOST as
``python3 ram_probe.py compose-sizing`` (pure stdlib, no teatree import needed on
that path), it emits the worker container's derived ``cpus`` / ``mem_limit`` as
shell assignments for ``deploy/deploy.sh`` to ``eval``. Deriving on the uncapped
host is the whole point: the compose caps then reflect the REAL host, so inside
the cgroup-capped worker :func:`available_cpu_count` reads a host-sized quota
instead of a baked-in 3-core cap that made host-derived concurrency a no-op.
"""

import os
import platform
import re
import sys

# Reserve for the light sibling containers (admin 512m + slack-listener 512m +
# watchdog 128m ≈ 1.25 GiB) carved off host RAM before sizing the worker.
_SIBLING_RESERVE_MIB = 1280
# Keep ~20% of host RAM for the OS / page cache / short bursts — the worker gets
# the rest as a hard ``mem_limit`` (a cgroup OOM ceiling, so headroom matters).
_HOST_HEADROOM = 0.8
# Never cap the worker below 2 GiB even on a tiny host (a single headless run needs it).
_WORKER_MIN_MIB = 2048
# ``vm_stat`` states its own page size on the header line; only fall back to the
# Intel-era 4 KiB when that line cannot be read (see :func:`_macos_page_size`).
_VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_DEFAULT_PAGE_SIZE = 4096


def _macos_page_size(vm_stat_output: str) -> int:
    """Page size (bytes) declared by ``vm_stat``'s header, or 4096 when unreadable.

    Not a constant: Apple Silicon uses 16384-byte pages where Intel used 4096,
    and ``vm_stat`` reports page COUNTS. Assuming 4 KiB on an arm64 Mac scales
    the reclaimable total (free + inactive pages) down 4x, so a 24 GiB host with
    ~7 GB free reads back as ~93% used instead of ~70%. That phantom pressure
    made :func:`teatree.core.gates.provision_admission_gate.check_provision_admission`
    hold every ``worktree start`` against its 85% ceiling. ``vm_stat`` states the
    real size on its first line, so read it rather than guess.

    The 4096 fallback keeps an unparsable header on the historical arithmetic
    instead of raising — consistent with this module degrading rather than
    crashing a caller.
    """
    match = _VM_STAT_PAGE_SIZE_RE.search(vm_stat_output)
    return int(match.group(1)) if match else _DEFAULT_PAGE_SIZE


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
    page_size, free_pages, inactive_pages = _macos_page_size(stat), 0, 0
    for line in stat.splitlines():
        if line.startswith("Pages free:"):
            free_pages = int(line.split(":")[1].strip().rstrip("."))
        elif line.startswith("Pages inactive:"):
            inactive_pages = int(line.split(":")[1].strip().rstrip("."))
    used = total - (free_pages + inactive_pages) * page_size
    return max(0.0, min(100.0, used * 100.0 / total))


def _linux_ram_used_percent() -> float:
    """Read Linux RAM use via ``/proc/meminfo`` (mirrors statusline.sh)."""
    info = _linux_meminfo()
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    if total <= 0:
        return 0.0
    used = total - avail
    return max(0.0, min(100.0, used * 100.0 / total))


def _linux_meminfo() -> dict[str, int]:
    """Parse ``/proc/meminfo`` into ``{key: kB}``; empty on any read failure."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:  # noqa: PTH123 — builtin open on a device/proc path; Path.open adds nothing here
            lines = handle.readlines()
    except OSError:
        return {}
    info: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        value = rest.strip().split(" ", 1)[0]
        if value.isdigit():
            info[key.strip()] = int(value)
    return info


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


def host_total_ram_mib() -> int:
    """Physical RAM of the host in whole MiB, or ``0`` when it cannot be read.

    Linux reads ``/proc/meminfo``'s ``MemTotal`` (kB); macOS reads
    ``sysctl -n hw.memsize`` (bytes). ``0`` on any failure so a caller keeps its
    fallback rather than sizing a cap off a bogus reading.
    """
    system = platform.system()
    if system == "Linux":
        return _linux_meminfo().get("MemTotal", 0) // 1024
    if system == "Darwin":
        return _macos_total_ram_mib()
    return 0


def _macos_total_ram_mib() -> int:
    """Read mac physical RAM (MiB) via ``sysctl -n hw.memsize``; ``0`` on failure."""
    import shutil  # noqa: PLC0415 — deferred: loaded only on this code path

    from teatree.utils.run import CommandFailedError, run_checked  # noqa: PLC0415 — deferred: call-time import

    sysctl = shutil.which("sysctl")
    if not sysctl:
        return 0
    try:
        total = int(run_checked([sysctl, "-n", "hw.memsize"], timeout=2).stdout.strip())
    except (CommandFailedError, ValueError, OSError, TimeoutError):
        return 0
    return max(0, total // (1024 * 1024))


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
    not the host (#3409). With the worker's cgroup cap now derived from the real
    host at deploy time (:func:`derive_worker_cpus`, #3432), that cap reflects
    the host — the derivation is no longer a no-op inside the capped worker.
    """
    n = cpu_count if cpu_count is not None else available_cpu_count()
    return max(1, n // 2)


def derive_worker_cpus(cpu_count: int | None = None) -> int:
    """Whole-core CPU quota for the worker container, derived from the host (#3432).

    All host cores but one — the reserved core covers the light
    admin/listener/watchdog sidecars and the host OS — floored at 1. Called by
    ``deploy/deploy.sh`` on the UNCAPPED host so the resulting compose ``cpus``
    reflects real host cores; inside the cgroup-capped worker
    :func:`available_cpu_count` then reads this quota and
    :func:`default_provision_concurrency` derives concurrency from the host
    instead of a baked-in 3-core cap.
    """
    n = cpu_count if cpu_count is not None else available_cpu_count()
    return max(1, n - 1)


def derive_worker_mem_limit_mib(total_ram_mib: int | None = None) -> int:
    """Worker ``mem_limit`` in whole MiB derived from host RAM, or ``0`` when unknown (#3432).

    Host RAM minus a fixed reserve for the sibling containers
    (:data:`_SIBLING_RESERVE_MIB`) and ~20% OS/burst headroom
    (:data:`_HOST_HEADROOM`), floored at :data:`_WORKER_MIN_MIB`. Returns ``0``
    when host RAM is unreadable so ``deploy/deploy.sh`` keeps the compose default
    rather than imposing a cap derived from a bogus reading.
    """
    total = total_ram_mib if total_ram_mib is not None else host_total_ram_mib()
    if total <= 0:
        return 0
    worker = int((total - _SIBLING_RESERVE_MIB) * _HOST_HEADROOM)
    return max(_WORKER_MIN_MIB, worker)


def _emit_compose_sizing() -> None:
    """Print the worker's deploy-derived compose caps as shell ``KEY=VALUE`` lines.

    ``deploy/deploy.sh`` ``eval``s this on the host before ``docker compose up``.
    The ``mem_limit`` line is omitted when host RAM is unreadable so compose keeps
    its in-file default; ``cpus`` always emits (its derivation floors at 1).
    """
    sys.stdout.write(f"TEATREE_WORKER_CPUS={derive_worker_cpus()}\n")
    mem = derive_worker_mem_limit_mib()
    if mem > 0:
        sys.stdout.write(f"TEATREE_WORKER_MEM_LIMIT={mem}m\n")


__all__ = [
    "available_cpu_count",
    "default_provision_concurrency",
    "derive_worker_cpus",
    "derive_worker_mem_limit_mib",
    "host_total_ram_mib",
    "read_ram_used_percent",
]


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "compose-sizing":
        _emit_compose_sizing()
    else:
        sys.stderr.write("usage: ram_probe.py compose-sizing\n")
        raise SystemExit(2)
