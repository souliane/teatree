"""Resource-pressure freeing handler — the executor for ``resource.cleanup_needed`` (#128).

Split out of :mod:`teatree.loop.mechanical` so the ladder (cache purge,
Docker disk reclaim, dormant-artifact eviction, the done-worktree sweep,
idle-container stop) lives in one self-describing module; ``mechanical.py`` registers the entry
point in ``HANDLERS``.

The heuristic worktree GC and process SIGTERM levers were removed. The remaining
worktree sweep verifies completed tickets before reclaiming their checkouts.

Docker disk reclaim: build cache and unused images are typically the largest
reclaimable consumers on a host that builds often, and file-cache purging alone
does not touch them. The disk ladder routes the sanctioned
:func:`teatree.docker.reclaim.reclaim_disk` (build cache + DANGLING-only images
+ UNREFERENCED-only volumes via a FIXED argv that can never contain ``-a`` /
``system prune``), so a running container's images, a tagged application image,
and an attached DB volume backing a live worktree all survive. It is
non-destructive by construction, so — like the cache purge and ``uv cache
prune`` — it runs without a destructive gate.

Contract — every step is dry-run-first and best-effort. (1) Compute the
freeing *plan* (candidate paths/targets + byte estimates) and persist it to
``ResourcePressureMarker.last_plan`` BEFORE executing, so the plan is recorded
before a safe reclaim begins. The loss-free artifact sweep is its own pass in :mod:`teatree.loop.mechanical_artifacts`,
recording to its own marker field; the plan primitives both share are in
:mod:`teatree.loop.mechanical_plan`.
(2) Execute only the safe steps, skipping on any ambiguity.
(3) Every subprocess / IO failure
is swallowed and logged — a cleanup failure can never crash the tick (mirrors
``SelfUpdateScanner._record_marker``). (4) Re-measure after a freeing pass and
stamp ``last_freed_at`` so the scanner's anti-thrash rate-limit holds.

Hard guards (never bypassable): ``~/.claude/projects`` (session memory) is
NEVER purged at any level; ``~/.cache/prek`` is NEVER auto-purged (unknown
rebuild semantics) unless the user explicitly lists it in
``disk_cache_allowlist``; the active session's worktree (CWD) and the
claude-CLI process ancestry are never reclaim candidates.
"""

import logging
import shutil
from pathlib import Path

from django.utils import timezone

from teatree.config import worktree_root
from teatree.core.cleanup.disk_usage import dir_size_gb
from teatree.docker.reclaim import reclaim_disk
from teatree.loop.dispatch import ActionPayload
from teatree.loop.mechanical_plan import GIB, FreePlan, persist_plan
from teatree.loop.reclaim_yield import reclaim_yield_steps
from teatree.utils.run import CommandFailedError, run_allowed_to_fail

logger = logging.getLogger(__name__)

# Paths that must NEVER be auto-removed regardless of the allow-list, because
# they hold irreplaceable state. ``~/.claude/projects`` is session memory.
_PROTECTED_DISK_PATHS: tuple[str, ...] = ("~/.claude/projects",)

_STALE_STATUSLINE_DAYS = 2

# The well-known statusline scratch dir. A module constant so it is patchable
# in tests without monkeypatching ``pathlib.Path`` itself.
_STATUSLINE_DIR = Path("/tmp/claude-statusline")  # noqa: S108 — fixed agent-controlled path, not user input


def free_resources(payload: ActionPayload) -> None:
    """Run the freeing ladder for the ``resource.cleanup_needed`` signal.

    A step that raises ends the pass there: ``_execute_mechanical`` records it in the
    tick's errors, and the plan persisted before execution survives on the marker.
    """
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker  # noqa: PLC0415 — lazy ORM import

    resource = str(payload.get("resource", ""))
    if resource == "disk":
        plan = _plan_disk(payload)
    elif resource == "ram":
        plan = _plan_ram()
    else:
        logger.warning("free_resources: unknown resource %r — nothing to do", resource)
        return

    marker = ResourcePressureMarker.load()
    persist_plan(marker, plan, field_name="last_plan", caller="free_resources")
    _execute_plan(plan, payload)
    if resource == "disk":
        plan.steps.extend(reclaim_yield_steps(marker, reclaimed_gb=plan.reclaimed_gb, payload=payload))
    persist_plan(marker, plan, field_name="last_plan", caller="free_resources")
    marker.last_freed_at = timezone.now()
    marker.save(update_fields=["last_freed_at", "last_plan"])
    logger.info("free_resources(%s) reclaimed ~%.2f GB", resource, plan.reclaimed_gb)


# ---------------------------------------------------------------------------
# Disk ladder
# ---------------------------------------------------------------------------


def _plan_disk(payload: ActionPayload) -> FreePlan:
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
    plan.steps.append("RECLAIM docker build cache + dangling images + unreferenced volumes (safe, never -a)")
    plan.steps.append("REAP worktrees whose ticket is done and whose every change is redundant")
    return plan


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


def _execute_disk(plan: FreePlan, payload: ActionPayload) -> None:
    for path in _resolve_disk_allowlist(payload):
        plan.reclaimed_gb += _purge_dir(path)
    _run_uv_cache_prune()
    _clean_stale_statusline()
    plan.reclaimed_gb += _reclaim_docker_disk(plan)
    _reap_done_worktrees(plan)


def _reap_done_worktrees(plan: FreePlan) -> None:
    """Run the analyze-then-wipe sweep for worktrees whose ticket is already done.

    The one reclaim on this box that demonstrably works was reachable only by a
    human typing ``workspace clean-merged``, so merged worktrees accumulated
    between the moments somebody remembered. This sweep wipes only
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
    A venue that cannot reach dockerd ran nothing, so it says so rather than
    logging a 0B reclaim the next reader takes for "nothing to reclaim" (#4585).
    """
    try:
        report = reclaim_disk()
    except Exception:
        logger.exception("free_resources: docker disk reclaim failed — swallowed")
        return 0.0
    if report.venue_blocked:
        plan.steps.append(f"  → docker reclaim did not run: {report.failure_summary()}")
        return 0.0
    reclaimed_gb = report.total_bytes / GIB
    plan.steps.append(f"  → docker reclaimed {report.total_human}")
    return reclaimed_gb


def _purge_dir(path: str) -> float:
    """Remove the cache DIRECTORY itself; return GB reclaimed (best-effort).

    ``rmtree`` takes the directory, not just its contents, so an allow-list entry
    is only safe for a cache whose owner recreates its own root on next use.
    """
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


# ---------------------------------------------------------------------------
# RAM ladder
# ---------------------------------------------------------------------------


def _plan_ram() -> FreePlan:
    plan = FreePlan(resource="ram")
    idle = _idle_containers()
    for cid in idle:
        plan.steps.append(f"STOP/prune idle container {cid}")
    plan.steps.append("RUN docker container prune -f (exited only)")
    return plan


def _execute_ram() -> None:
    for cid in _idle_containers():
        _stop_container(cid)
    _docker_container_prune()


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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _execute_plan(plan: FreePlan, payload: ActionPayload) -> None:
    if plan.resource == "disk":
        _execute_disk(plan, payload)
    else:
        _execute_ram()


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


__all__ = ["FreePlan", "free_resources"]
