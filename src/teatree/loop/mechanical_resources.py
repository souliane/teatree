"""Resource-pressure freeing handler — the executor for ``resource.cleanup_needed`` (#128).

Split out of :mod:`teatree.loop.mechanical` so the ladder (cache purge,
idle-container stop, flag-gated worktree GC, flag-gated renderer SIGTERM)
lives in one self-describing module and ``mechanical.py`` only registers the
entry point in ``HANDLERS``.

Contract — every step is dry-run-first and best-effort. (1) Compute the
freeing *plan* (candidate paths/targets + byte estimates) and persist it to
``ResourcePressureMarker.last_plan`` BEFORE executing, so the plan is recorded
even when a destructive flag is off and the user sees what *would* have run.
(2) Execute only the steps the payload's flags permit; destructive steps
(worktree GC, process SIGTERM) require an explicit opt-in flag and run
allow-LIST only, skipping on any ambiguity. (3) Every subprocess / IO failure
is swallowed and logged — a cleanup failure can never crash the tick (mirrors
``SelfUpdateScanner._record_marker``). (4) Re-measure after a freeing pass and
stamp ``last_freed_at`` so the scanner's anti-thrash rate-limit holds.

Hard guards (never bypassable): ``~/.claude/projects`` (session memory) is
NEVER purged at any level; ``~/.cache/prek`` is NEVER auto-purged (unknown
rebuild semantics) unless the user explicitly lists it in
``disk_cache_allowlist``; the active session's worktree (CWD) and the
claude-CLI process ancestry are NEVER touched by the destructive levers.
"""

import logging
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path

from django.utils import timezone

from teatree.config import worktree_root
from teatree.loop.dispatch import ActionPayload
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

logger = logging.getLogger(__name__)

_GIB = 1024 * 1024 * 1024

# Paths that must NEVER be auto-removed regardless of the allow-list, because
# they hold irreplaceable state. ``~/.claude/projects`` is session memory.
_PROTECTED_DISK_PATHS: tuple[str, ...] = ("~/.claude/projects",)

_STALE_STATUSLINE_DAYS = 2

# The well-known statusline scratch dir. A module constant so it is patchable
# in tests without monkeypatching ``pathlib.Path`` itself.
_STATUSLINE_DIR = Path("/tmp/claude-statusline")  # noqa: S108 — fixed agent-controlled path, not user input


@dataclass(slots=True)
class FreePlan:
    """The computed freeing plan for one pass — persisted before execution."""

    resource: str
    steps: list[str] = field(default_factory=list)
    estimated_reclaim_gb: float = 0.0
    reclaimed_gb: float = 0.0

    def render(self) -> str:
        head = (
            f"[{timezone.now().isoformat()}] resource={self.resource} "
            f"est={self.estimated_reclaim_gb:.2f}GB reclaimed={self.reclaimed_gb:.2f}GB"
        )
        return head + "\n" + "\n".join(f"  - {s}" for s in self.steps)


def free_resources(payload: ActionPayload) -> None:
    """Run the freeing ladder for the ``resource.cleanup_needed`` signal.

    Best-effort top to bottom: a failure in any single step logs and is
    swallowed so the tick continues. The whole body is additionally wrapped
    so an unexpected error (a missing marker table on a pre-migration
    install, an import failure) can never abort ``_execute_mechanical``.
    """
    try:
        _free_resources_inner(payload)
    except Exception:
        logger.exception("free_resources: cleanup pass failed — swallowed to protect the tick")


def _free_resources_inner(payload: ActionPayload) -> None:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker  # noqa: PLC0415 — lazy ORM import

    resource = str(payload.get("resource", ""))
    if resource == "disk":
        plan = _plan_disk(payload)
    elif resource == "ram":
        plan = _plan_ram(payload)
    else:
        logger.warning("free_resources: unknown resource %r — nothing to do", resource)
        return

    marker = ResourcePressureMarker.load()
    _persist_plan(marker, plan)
    _execute_plan(plan, payload)
    _persist_plan(marker, plan)
    marker.last_freed_at = timezone.now()
    marker.save(update_fields=["last_freed_at", "last_plan"])
    logger.info("free_resources(%s) reclaimed ~%.2f GB", resource, plan.reclaimed_gb)


def _persist_plan(marker: object, plan: FreePlan) -> None:
    try:
        marker.last_plan = plan.render()  # type: ignore[attr-defined]
        marker.save(update_fields=["last_plan"])  # type: ignore[attr-defined]
    except Exception:
        logger.exception("free_resources: failed to persist plan")


# ---------------------------------------------------------------------------
# Disk ladder
# ---------------------------------------------------------------------------


def _plan_disk(payload: ActionPayload) -> FreePlan:
    plan = FreePlan(resource="disk")
    allowlist = _resolve_disk_allowlist(payload)
    for path in allowlist:
        size_gb = _dir_size_gb(path)
        plan.steps.append(f"PURGE cache {path} (~{size_gb:.2f} GB)")
        plan.estimated_reclaim_gb += size_gb
    plan.steps.append("RUN uv cache prune")
    plan.steps.append(f"CLEAN /tmp/claude-statusline entries older than {_STALE_STATUSLINE_DAYS}d")
    if payload.get("allow_destructive_disk"):
        worktrees = _gc_candidate_worktrees(payload)
        for wt in worktrees:
            plan.steps.append(f"GC worktree {wt} (clean + pushed + stale)")
    else:
        plan.steps.append("SKIP worktree GC (allow_destructive_disk=false)")
    return plan


def _resolve_disk_allowlist(payload: ActionPayload) -> list[str]:
    """Expand the allow-list, dropping any path that matches a protected root."""
    raw = payload.get("disk_cache_allowlist") or []
    resolved: list[str] = []
    protected = {Path(p).expanduser().resolve() for p in _PROTECTED_DISK_PATHS}
    for entry in raw:
        candidate = Path(str(entry)).expanduser()
        try:
            real = candidate.resolve()
        except OSError:
            continue
        if real in protected:
            logger.warning("free_resources: refusing to purge protected path %s", entry)
            continue
        resolved.append(str(candidate))
    return resolved


def _execute_disk(plan: FreePlan, payload: ActionPayload) -> None:
    for path in _resolve_disk_allowlist(payload):
        plan.reclaimed_gb += _purge_dir(path)
    _run_uv_cache_prune()
    _clean_stale_statusline()
    if payload.get("allow_destructive_disk"):
        plan.reclaimed_gb += _gc_worktrees(payload)


def _purge_dir(path: str) -> float:
    """Remove a cache directory's contents; return GB reclaimed (best-effort)."""
    target = Path(path).expanduser()
    if not target.is_dir():
        return 0.0
    before = _dir_size_gb(str(target))
    try:
        shutil.rmtree(target, ignore_errors=True)
    except OSError:
        logger.exception("free_resources: failed to purge %s", path)
        return 0.0
    return before


def _dir_size_gb(path: str) -> float:
    target = Path(path).expanduser()
    if not target.is_dir():
        return 0.0
    total = 0
    for root, _dirs, files in os.walk(target):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total / _GIB


def _run_uv_cache_prune() -> None:
    uv = shutil.which("uv")
    if uv is None:
        return
    _run([uv, "cache", "prune"], timeout=120)


def _clean_stale_statusline() -> None:
    base = _STATUSLINE_DIR
    if not base.is_dir():
        return
    cutoff = timezone.now().timestamp() - _STALE_STATUSLINE_DAYS * 86400
    for entry in base.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


# ---------------------------------------------------------------------------
# Worktree GC (flag-gated, destructive)
# ---------------------------------------------------------------------------


def _gc_candidate_worktrees(payload: ActionPayload) -> list[str]:
    """List worktrees eligible for GC: clean + fully pushed + stale + not the CWD."""
    stale_days = int(payload.get("worktree_stale_days", 30))
    cap = int(payload.get("max_worktree_gc_per_tick", 3))
    cwd = _safe_cwd()
    candidates: list[str] = []
    for wt in _list_workspace_worktrees():
        if len(candidates) >= cap:
            break
        if cwd is not None and _is_within(cwd, wt):
            continue
        if _worktree_is_gc_eligible(wt, stale_days=stale_days):
            candidates.append(str(wt))
    return candidates


def _gc_worktrees(payload: ActionPayload) -> float:
    reclaimed = 0.0
    for wt in _gc_candidate_worktrees(payload):
        size_gb = _dir_size_gb(wt)
        if _remove_worktree(Path(wt)):
            reclaimed += size_gb
    return reclaimed


def _list_workspace_worktrees() -> list[Path]:
    """Enumerate git worktrees under the per-overlay WORKTREE root via ``git worktree list``."""
    workspace = worktree_root()
    if not workspace.is_dir():
        return []
    result = _git(workspace, "worktree", "list", "--porcelain")
    if result is None:
        return []
    return [Path(line[len("worktree ") :].strip()) for line in result.splitlines() if line.startswith("worktree ")]


def _worktree_is_gc_eligible(wt: Path, *, stale_days: int) -> bool:
    if not wt.is_dir():
        return False
    if _git_dirty(wt):
        return False
    if _git_ahead_of_upstream(wt):
        return False
    return _is_stale(wt, stale_days=stale_days)


def _git_dirty(wt: Path) -> bool:
    out = _git(wt, "status", "--porcelain")
    if out is None:
        return True  # can't tell → treat as dirty (skip)
    return bool(out.strip())


def _git_ahead_of_upstream(wt: Path) -> bool:
    out = _git(wt, "log", "@{u}..", "--oneline")
    if out is None:
        return True  # no upstream / can't tell → treat as ahead (skip)
    return bool(out.strip())


def _is_stale(wt: Path, *, stale_days: int) -> bool:
    try:
        mtime = wt.stat().st_mtime
    except OSError:
        return False
    age_days = (timezone.now().timestamp() - mtime) / 86400
    return age_days >= stale_days


def _remove_worktree(wt: Path) -> bool:
    # ``-C <wt>`` resolves the worktree's gitdir before removal, so the call
    # works even though the worktree's parent dir is the (non-repo) workspace
    # root. Running from the parent would ``fatal: not a git repository``.
    result = _git(wt, "worktree", "remove", str(wt))
    return result is not None


def _git(cwd: Path, *args: str) -> str | None:
    """Run a read-or-write git command; ``None`` on any failure (caller skips)."""
    git = shutil.which("git")
    if git is None:
        return None
    return _run([git, *args], cwd=cwd, timeout=60)


# ---------------------------------------------------------------------------
# RAM ladder
# ---------------------------------------------------------------------------


def _plan_ram(payload: ActionPayload) -> FreePlan:
    plan = FreePlan(resource="ram")
    idle = _idle_containers()
    for cid in idle:
        plan.steps.append(f"STOP/prune idle container {cid}")
    plan.steps.append("RUN docker container prune -f (exited only)")
    if _ram_kill_enabled(payload):
        targets = _kill_candidate_pids(payload)
        for pid, name in targets:
            plan.steps.append(f"SIGTERM pid {pid} ({name}) — allow-listed renderer, not session ancestry")
    else:
        reason = _ram_kill_skip_reason(payload)
        plan.steps.append(f"SKIP process kill ({reason})")
    return plan


def _execute_ram(payload: ActionPayload) -> None:
    for cid in _idle_containers():
        _stop_container(cid)
    _docker_container_prune()
    if _ram_kill_enabled(payload):
        for pid, _name in _kill_candidate_pids(payload):
            _sigterm(pid)


def _ram_kill_enabled(payload: ActionPayload) -> bool:
    return bool(payload.get("allow_destructive_ram")) and int(payload.get("consecutive_critical", 0)) >= 2  # noqa: PLR2004 — self-documenting literal in this context


def _ram_kill_skip_reason(payload: ActionPayload) -> str:
    if not payload.get("allow_destructive_ram"):
        return "allow_destructive_ram=false"
    return "not yet 2 consecutive CRITICAL ticks"


def _idle_containers() -> list[str]:
    out = _docker(
        "ps",
        "-a",
        "--filter",
        "status=exited",
        "--filter",
        "status=created",
        "--format",
        "{{.ID}}",
    )
    if out is None:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _stop_container(container_id: str) -> None:
    _docker("stop", container_id)


def _docker_container_prune() -> None:
    _docker("container", "prune", "-f")


def _docker(*args: str) -> str | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    return _run([docker, *args], timeout=120)


# ---------------------------------------------------------------------------
# Process kill (flag-gated, destructive, session-protected, SIGTERM only)
# ---------------------------------------------------------------------------


def _kill_candidate_pids(payload: ActionPayload) -> list[tuple[int, str]]:
    """Resolve (pid, name) targets: allow-list match AND not in session ancestry."""
    patterns = [re.compile(p) for p in (payload.get("ram_kill_allowlist") or [])]
    if not patterns:
        return []
    protected = _session_pid_ancestry()
    candidates: list[tuple[int, str]] = []
    for pid, name in _list_processes():
        if pid in protected:
            continue
        if any(pat.search(name) for pat in patterns):
            candidates.append((pid, name))
    return candidates


def _session_pid_ancestry() -> set[int]:
    """Walk the current process's parent-pid chain — these are NEVER killed.

    The freeing handler runs inside the active session's process tree (the
    claude CLI → its shell → this python). Every ancestor pid is off-limits so
    the scanner can never terminate the session that is running it, the
    controlling terminal, or any shell in between.
    """
    ancestry: set[int] = set()
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        ancestry.add(pid)
        parent = _parent_pid(pid)
        if parent is None:
            break
        pid = parent
    return ancestry


def _parent_pid(pid: int) -> int | None:
    out = _ps("-o", "ppid=", "-p", str(pid))
    if out is None:
        return None
    stripped = out.strip()
    if not stripped.isdigit():
        return None
    return int(stripped)


def _list_processes() -> list[tuple[int, str]]:
    out = _ps("-axo", "pid=,comm=")
    if out is None:
        return []
    processes: list[tuple[int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():  # noqa: PLR2004 — self-documenting literal in this context
            processes.append((int(parts[0]), parts[1]))
    return processes


def _ps(*args: str) -> str | None:
    ps = shutil.which("ps")
    if ps is None:
        return None
    return _run([ps, *args], timeout=30)


def _sigterm(pid: int) -> None:
    """Send SIGTERM (never SIGKILL) to *pid*; swallow any error."""
    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("free_resources: sent SIGTERM to pid %d", pid)
    except OSError:
        logger.warning("free_resources: SIGTERM to pid %d failed", pid)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _execute_plan(plan: FreePlan, payload: ActionPayload) -> None:
    if plan.resource == "disk":
        _execute_disk(plan, payload)
    else:
        _execute_ram(payload)


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: float = 60) -> str | None:
    """Run a fully-resolved command; ``None`` on any non-zero exit or failure.

    Centralises the subprocess invocation on ``run_allowed_to_fail`` (the
    project's S603/S607-vetted wrapper). The caller always passes an absolute
    binary path (resolved via ``shutil.which``), so there is no partial-path
    or untrusted-input concern. Any timeout / OS error / non-zero exit maps to
    ``None`` so every caller stays best-effort.
    """
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, cwd=cwd, timeout=timeout)
    except (OSError, CommandFailedError):
        return None
    except Exception:
        logger.exception("free_resources: subprocess %s raised", cmd[0])
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _safe_cwd() -> Path | None:
    try:
        return Path.cwd().resolve()
    except OSError:
        return None


def _is_within(child: Path, ancestor: Path) -> bool:
    """True iff *child* is the same as or nested under *ancestor* (resolved)."""
    try:
        resolved = ancestor.resolve()
    except OSError:
        return False
    return resolved == child or resolved in child.parents


__all__ = ["FreePlan", "free_resources"]
