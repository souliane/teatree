"""Resource-pressure freeing handler — the executor for ``resource.cleanup_needed`` (#128).

Split out of :mod:`teatree.loop.mechanical` so the ladder (cache purge,
Docker disk reclaim, dormant-venv eviction, the done-worktree sweep,
idle-container stop, flag-gated worktree GC, flag-gated renderer SIGTERM) lives
in one self-describing module and ``mechanical.py`` only registers the entry
point in ``HANDLERS``.

WHAT A PASS RECLAIMS, AND WHY IT USED TO BE ONLY DOCKER (#4244). The worktree GC
enumerated by running ``git worktree list`` against the worktree ROOT — a
directory that CONTAINS worktrees and is not a repository — so git refused, the
helper mapped the refusal to ``[]``, and the pass reclaimed nothing but ~1.6 GB
of rebuildable docker cache while tens of gigabytes of dormant virtualenvs
accumulated. Enumeration now runs through
:func:`teatree.core.cleanup.checkout_registry.linked_worktree_paths`, an
unreadable answer is an ERROR line rather than an empty candidate list, and the
plan reports considered/eligible/kept counts so a GC that reclaims nothing is
visible instead of inferred.

Docker disk reclaim: build cache and unused images are typically the largest
reclaimable consumers on a host that builds often, and file-cache purging alone
does not touch them. The disk ladder routes the sanctioned
:func:`teatree.docker.reclaim.reclaim_disk` (build cache + DANGLING-only images
+ UNREFERENCED-only volumes via a FIXED argv that can never contain ``-a`` /
``system prune``), so a running container's images, a tagged application image,
and an attached DB volume backing a live worktree all survive. It is
non-destructive by construction, so — like the cache purge and ``uv cache
prune`` — it runs WITHOUT the ``allow_destructive_disk`` flag.

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
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.config import worktree_root
from teatree.core.cleanup.disk_usage import dir_size_gb
from teatree.core.cleanup.venv_eviction import VenvEvictionPlan, evict_venvs, plan_venv_eviction
from teatree.core.retention.scratch import resolve_scratch_sweep, sweep_scratch
from teatree.docker.reclaim import reclaim_disk
from teatree.loop.dispatch import ActionPayload
from teatree.loop.worktree_gc import GcSurvey, collect, survey_worktrees
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker

logger = logging.getLogger(__name__)

_GIB = 1024 * 1024 * 1024

# How many per-item lines one plan section prints before summarising the rest.
# The plan is read by a human on a full disk; hundreds of keep-lines would bury
# the counts that say whether the pass did anything.
_PLAN_SAMPLE = 5

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
    survey: DiskSurvey | None = None
    if resource == "disk":
        survey = _survey_disk(payload)
        plan = _plan_disk(payload, survey)
    elif resource == "ram":
        plan = _plan_ram(payload)
    else:
        logger.warning("free_resources: unknown resource %r — nothing to do", resource)
        return

    marker = ResourcePressureMarker.load()
    _persist_plan(marker, plan)
    _execute_plan(plan, payload, survey)
    _persist_plan(marker, plan)
    marker.last_freed_at = timezone.now()
    marker.save(update_fields=["last_freed_at", "last_plan"])
    logger.info("free_resources(%s) reclaimed ~%.2f GB", resource, plan.reclaimed_gb)


def _persist_plan(marker: "ResourcePressureMarker", plan: FreePlan) -> None:
    try:
        marker.last_plan = plan.render()
        marker.save(update_fields=["last_plan"])
    except Exception:
        logger.exception("free_resources: failed to persist plan")


# ---------------------------------------------------------------------------
# Disk ladder
# ---------------------------------------------------------------------------


def _plan_disk(payload: ActionPayload, survey: "DiskSurvey") -> FreePlan:
    plan = FreePlan(resource="disk")
    for path in _resolve_disk_allowlist(payload):
        # An entry that names nothing is reported as ABSENT rather than as a
        # 0.00 GB purge: the two read identically in the plan, so a stale
        # allow-list (every shipped default was absent on the host that produced
        # #3852) looked exactly like a cache that was already clean, and the
        # CRITICAL alarm re-fired every tick with no way to see why it reclaimed
        # nothing.
        if not Path(path).expanduser().is_dir():
            plan.steps.append(f"SKIP cache {path} (absent — nothing here to reclaim; is this entry stale?)")
            continue
        size_gb = _dir_size_gb(path)
        plan.steps.append(f"PURGE cache {path} (~{size_gb:.2f} GB)")
        plan.estimated_reclaim_gb += size_gb
    plan.steps.append("RUN uv cache prune")
    plan.steps.append(f"CLEAN /tmp/claude-statusline entries older than {_STALE_STATUSLINE_DAYS}d")
    plan.steps.append(_scratch_plan_step(payload))
    plan.steps.append("RECLAIM docker build cache + dangling images + unreferenced volumes (safe, never -a)")
    _append_venv_steps(plan, survey.venvs)
    plan.steps.append("REAP worktrees whose ticket is done and whose every change is redundant")
    _append_gc_steps(plan, survey.gc, allowed=bool(payload.get("allow_destructive_disk")))
    return plan


def _append_venv_steps(plan: FreePlan, eviction: VenvEvictionPlan) -> None:
    if eviction.refusal:
        plan.steps.append(f"SKIP venv eviction — {eviction.refusal}")
        return
    plan.steps.append(
        f"EVICT dormant venvs: considered={eviction.considered} evicting={len(eviction.candidates)} "
        f"kept={len(eviction.kept)}"
    )
    for line in _sampled(eviction.kept):
        plan.steps.append(f"  keep {line}")
    for gap in _sampled(eviction.gaps):
        plan.steps.append(f"  ERROR checkout enumeration incomplete — {gap}")
    plan.estimated_reclaim_gb += eviction.estimated_bytes / _GIB


def _append_gc_steps(plan: FreePlan, survey: "GcSurvey", *, allowed: bool) -> None:
    if survey.refusal:
        plan.steps.append(f"SKIP worktree GC — {survey.refusal}")
        return
    plan.steps.append(
        f"GC worktrees: considered={survey.considered} eligible={len(survey.candidates)} kept={len(survey.kept)}"
    )
    for line in _sampled(survey.kept):
        plan.steps.append(f"  keep {line}")
    for gap in _sampled(survey.gaps):
        plan.steps.append(f"  ERROR worktree enumeration incomplete — {gap}")
    if not allowed:
        plan.steps.append("SKIP worktree GC (allow_destructive_disk=false)")
        return
    for wt in survey.candidates:
        plan.steps.append(f"GC worktree {wt} (clean + pushed + stale + nothing running inside)")


def _sampled(lines: tuple[str, ...]) -> list[str]:
    """The first few lines plus an honest trailer — a plan nobody reads reports nothing."""
    if len(lines) <= _PLAN_SAMPLE:
        return list(lines)
    return [*lines[:_PLAN_SAMPLE], f"… and {len(lines) - _PLAN_SAMPLE} more"]


def _resolve_disk_allowlist(payload: ActionPayload) -> list[str]:
    """Expand the allow-list, dropping every entry that OVERLAPS a protected root.

    Containment is tested in both directions because ``_purge_dir`` is a
    recursive ``rmtree``: an entry INSIDE a protected path is part of the state
    that path protects, and an entry CONTAINING one takes it down with it. An
    exact-equality guard sees neither — ``~/.claude`` is not ``~/.claude/projects``.
    """
    raw = payload.get("disk_cache_allowlist") or []
    resolved: list[str] = []
    protected = {Path(p).expanduser().resolve() for p in _PROTECTED_DISK_PATHS}
    for entry in raw:
        candidate = Path(str(entry)).expanduser()
        try:
            real = candidate.resolve()
        except OSError:
            continue
        if any(_is_within(real, guarded) or _is_within(guarded, real) for guarded in protected):
            logger.warning("free_resources: refusing to purge protected path %s", entry)
            continue
        resolved.append(str(candidate))
    return resolved


def _execute_disk(plan: FreePlan, payload: ActionPayload, survey: "DiskSurvey") -> None:
    for path in _resolve_disk_allowlist(payload):
        plan.reclaimed_gb += _purge_dir(path)
    _run_uv_cache_prune()
    _clean_stale_statusline()
    plan.reclaimed_gb += _sweep_scratch(plan, payload)
    plan.reclaimed_gb += _reclaim_docker_disk(plan)
    eviction = evict_venvs(survey.venvs)
    plan.reclaimed_gb += eviction.freed_bytes / _GIB
    _append_stopped_deletions(plan, "venv eviction", eviction.refusal, eviction.skipped)
    _reap_done_worktrees(plan)
    if payload.get("allow_destructive_disk"):
        collection = collect(survey.gc)
        plan.reclaimed_gb += collection.reclaimed_gb
        _append_stopped_deletions(plan, "worktree GC", collection.refusal, collection.skipped)


def _append_stopped_deletions(plan: FreePlan, what: str, refusal: str, skipped: tuple[str, ...]) -> None:
    """Record what the delete-time guard stopped — a silent skip is the defect class itself."""
    if refusal:
        plan.steps.append(f"ABORT {what} at deletion time — {refusal}")
    for line in _sampled(skipped):
        plan.steps.append(f"  SKIP {line}")


def _reap_done_worktrees(plan: FreePlan) -> None:
    """Run the analyze-then-wipe sweep for worktrees whose ticket is already done.

    The one reclaim on this box that demonstrably works was reachable only by a
    human typing ``workspace clean-merged``, so merged worktrees accumulated
    between the moments somebody remembered. It runs without
    ``allow_destructive_disk`` because that flag guards the HEURISTIC GC below —
    clean-and-pushed-and-stale is an inference — whereas this sweep wipes only
    what it has proved done and redundant, the same predicate the FSM already
    applies unattended the moment a ticket merges.
    """
    from teatree.core.worktree.worktree_done import reap_done_worktrees  # noqa: PLC0415 — lazy ORM import

    try:
        reaped = reap_done_worktrees(worktree_root(), dry_run=False)
    except Exception:
        logger.exception("free_resources: done-worktree sweep failed — swallowed")
        plan.steps.append("  → done-worktree sweep failed (see logs)")
        return
    plan.steps.append(f"  → done-worktree sweep handled {len(reaped)} worktree row(s)")


def _reclaim_docker_disk(plan: FreePlan) -> float:
    """Run the safe Docker disk reclaim; return GB reclaimed (best-effort).

    Delegates to :func:`teatree.docker.reclaim.reclaim_disk` — the fixed-argv
    build-cache + dangling-image + unreferenced-volume prune that can never use
    ``-a``/``system prune``, so a running container's images are never reaped.
    A failure (no docker daemon, a prune error) is swallowed and recorded so
    the freeing pass continues; the per-step reclaimed bytes land in the plan.
    """
    try:
        report = reclaim_disk()
    except Exception:
        logger.exception("free_resources: docker disk reclaim failed — swallowed")
        return 0.0
    reclaimed_gb = report.total_bytes / _GIB
    plan.steps.append(f"  → docker reclaimed {report.total_human}")
    return reclaimed_gb


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
    return dir_size_gb(target) if target.is_dir() else 0.0


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


def _scratch_retention_days(payload: ActionPayload) -> int:
    return int(payload.get("scratch_retention_days", 0))


def _scratch_armed(payload: ActionPayload) -> bool:
    """A recursive unattended delete needs BOTH the window AND the destructive opt-in.

    The window alone armed it, which put an autonomous ``rmtree`` outside the very
    flag the worktree-GC lane beside it is gated on. An explicit human
    ``retention scratch --apply`` is its own authorization and is NOT gated here.
    """
    return _scratch_retention_days(payload) > 0 and bool(payload.get("allow_destructive_disk"))


def _scratch_plan_step(payload: ActionPayload) -> str:
    """The scratch lane's line in BOTH ladders — on a tmpfs /tmp this reclaims RAM, not disk."""
    days = _scratch_retention_days(payload)
    if days <= 0:
        return "SKIP agent-scratch sweep (scratch_retention_days=0)"
    if not payload.get("allow_destructive_disk"):
        return "SKIP agent-scratch sweep (allow_destructive_disk=false)"
    root = resolve_scratch_sweep(str(payload.get("scratch_sweep_root", ""))).root
    return f"SWEEP agent scratch under {root} older than {days}d"


def _sweep_scratch(plan: FreePlan, payload: ActionPayload) -> float:
    """Reclaim stale agent scratch; return GB freed. Best-effort, never raises."""
    if not _scratch_armed(payload):
        return 0.0
    try:
        swept = sweep_scratch(
            configured_root=str(payload.get("scratch_sweep_root", "")),
            retention_days=_scratch_retention_days(payload),
            apply=True,
        )
    except Exception:
        logger.exception("free_resources: agent-scratch sweep failed — swallowed")
        return 0.0
    if swept.refused:
        plan.steps.append(f"  → REFUSED agent-scratch sweep ({swept.probe_gap})")
        return 0.0
    plan.steps.append(f"  → {swept.summary}")
    return swept.reclaimed_bytes / _GIB


@dataclass(frozen=True, slots=True)
class DiskSurvey:
    """The disk ladder's read-only findings, computed once and used by plan and execute."""

    gc: GcSurvey
    venvs: VenvEvictionPlan


def _survey_disk(payload: ActionPayload) -> DiskSurvey:
    """Everything the disk ladder needs to look up, gathered once.

    The enumeration walks the box's checkouts, so planning it and then executing
    it from two independent surveys would pay for that walk twice and let the two
    disagree about what is on disk. Each half is independently best-effort: a
    survey that raises must cost its own step, never the docker reclaim and cache
    purge further up the ladder that had nothing to do with it.
    """
    return DiskSurvey(gc=_surveyed_worktrees(payload), venvs=_surveyed_venvs(payload))


def _surveyed_worktrees(payload: ActionPayload) -> GcSurvey:
    try:
        return survey_worktrees(payload)
    except Exception as exc:
        logger.exception("free_resources: worktree survey failed — swallowed")
        return GcSurvey(gaps=(f"the worktree survey raised ({exc})",))


def _surveyed_venvs(payload: ActionPayload) -> VenvEvictionPlan:
    try:
        return plan_venv_eviction(worktree_root(), idle_days=float(payload.get("venv_idle_days", 2)))
    except Exception as exc:
        logger.exception("free_resources: venv survey failed — swallowed")
        return VenvEvictionPlan(refusal=f"the venv survey raised ({exc})")


# ---------------------------------------------------------------------------
# RAM ladder
# ---------------------------------------------------------------------------


def _plan_ram(payload: ActionPayload) -> FreePlan:
    plan = FreePlan(resource="ram")
    idle = _idle_containers()
    for cid in idle:
        plan.steps.append(f"STOP/prune idle container {cid}")
    plan.steps.append("RUN docker container prune -f (exited only)")
    plan.steps.append(_scratch_plan_step(payload))
    if _ram_kill_enabled(payload):
        targets = _kill_candidate_pids(payload)
        for pid, name in targets:
            plan.steps.append(f"SIGTERM pid {pid} ({name}) — allow-listed renderer, not session ancestry")
    else:
        reason = _ram_kill_skip_reason(payload)
        plan.steps.append(f"SKIP process kill ({reason})")
    return plan


def _execute_ram(plan: FreePlan, payload: ActionPayload) -> None:
    for cid in _idle_containers():
        _stop_container(cid)
    _docker_container_prune()
    plan.reclaimed_gb += _sweep_scratch(plan, payload)
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


def _execute_plan(plan: FreePlan, payload: ActionPayload, survey: DiskSurvey | None) -> None:
    if plan.resource == "disk" and survey is not None:
        _execute_disk(plan, payload, survey)
    else:
        _execute_ram(plan, payload)


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


def _is_within(child: Path, ancestor: Path) -> bool:
    """True iff *child* is the same as or nested under *ancestor* (resolved)."""
    try:
        resolved = ancestor.resolve()
    except OSError:
        return False
    return resolved == child or resolved in child.parents


__all__ = ["DiskSurvey", "FreePlan", "free_resources"]
