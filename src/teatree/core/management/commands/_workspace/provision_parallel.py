"""Parallel cross-worktree provisioning for ``workspace provision`` (souliane/teatree#2949).

A ticket's worktrees provisioned one at a time paid the exact SUM of every
worktree's provision time. Each worktree's provision now runs as its OWN
subprocess under a bounded pool — never in-process threads, because
``WorktreeProvisionRunner``'s DB-import path mutates process-wide
``os.environ`` (``os.environ.update(...)`` in ``_run_db_import``), which two
concurrent in-process worktrees would clobber. A resource-aware admission
check runs before each new subprocess is submitted: a request that would push
host RAM over the ceiling is HELD (not started) and re-checked on the next
poll — it drains automatically once RAM frees, with no separate durable queue
needed for this single CLI invocation's lifetime.

Each subprocess invokes ``python -m teatree worktree provision --path
<wt_path>`` — the manage.py-equivalent entry point
(:func:`teatree.cli.overlay.managepy`'s pip-installed-overlay branch) — with
``T3_OVERLAY_NAME`` set in its environment, so the overlay resolves the same
way regardless of the overlay's registered top-level ``t3 <name>`` CLI group
name.
"""

import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from teatree.core.gates.provision_admission_gate import (
    ProvisionAdmissionVerdict,
    check_provision_admission,
    resolve_provision_max_concurrency,
)
from teatree.core.models import Worktree
from teatree.core.provision.step_runner import ProvisionReport
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)

# Ceiling for one worktree's ENTIRE provision subprocess (every step summed).
# Generous relative to the "explicit cold provision ≤ 35 min" acceptance
# target so a genuinely slow cold provision still completes rather than
# getting cut off mid-way.
DEFAULT_WORKTREE_PROVISION_TIMEOUT_SECONDS = 2400

# The "never hang silently" invariant (provision_timebox.py) applies to
# admission too: a RAM-held request drains automatically once RAM frees, but
# waits FOREVER only up to this ceiling — past it, admission is overridden and
# the submission proceeds anyway (logged loud), rather than a foreground `t3
# workspace provision` invocation blocking indefinitely on a host that never
# frees RAM within this CLI invocation's lifetime.
DEFAULT_MAX_ADMISSION_HOLD_SECONDS = 600


@dataclass(frozen=True, slots=True)
class WorktreeProvisionResult:
    """One worktree's outcome from a subprocess provision run."""

    worktree_id: int
    repo_path: str
    ok: bool
    detail: str


def _subprocess_env(overlay_name: str) -> dict[str, str]:
    """Build the child process's environment (mirrors ``teatree.cli.overlay.managepy``).

    Strips the parent's ``DJANGO_SETTINGS_MODULE`` (a worktree's own env cache
    may export a worktree-specific settings module that does not exist in this
    process) then pins the teatree core settings module explicitly, and sets
    ``T3_OVERLAY_NAME`` so ``get_overlay()`` resolves correctly regardless of
    which overlay's CLI group name would otherwise have selected it.
    """
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    env["DJANGO_SETTINGS_MODULE"] = "teatree.settings"
    if overlay_name:
        env["T3_OVERLAY_NAME"] = overlay_name
    return env


def provision_worktree_subprocess(
    worktree: Worktree,
    *,
    overlay_name: str,
    slow_import: bool,
    timeout: float = DEFAULT_WORKTREE_PROVISION_TIMEOUT_SECONDS,
) -> WorktreeProvisionResult:
    """Provision one worktree in an isolated OS subprocess.

    Invokes the existing single-worktree management command (``worktree
    provision --path <wt_path>``) via ``python -m teatree`` so the FSM
    transition + runner logic stays exactly as it is for a single worktree —
    only the CALLER (this module) changes, running several of these
    subprocesses concurrently instead of one ``for`` loop.
    """
    cmd = [sys.executable, "-m", "teatree", "worktree", "provision", "--path", worktree.worktree_path]
    if slow_import:
        cmd.append("--slow-import")
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, env=_subprocess_env(overlay_name), timeout=timeout)
    except TimeoutExpired:
        return WorktreeProvisionResult(
            worktree_id=worktree.pk,
            repo_path=worktree.repo_path,
            ok=False,
            detail=f"timed out after {timeout:.0f}s",
        )
    ok = result.returncode == 0
    tail_source = result.stdout if ok else (result.stderr or result.stdout)
    lines = [line for line in tail_source.strip().splitlines() if line.strip()]
    detail = lines[-1].strip() if lines else (f"exit code {result.returncode}" if not ok else "provisioned")
    return WorktreeProvisionResult(worktree_id=worktree.pk, repo_path=worktree.repo_path, ok=ok, detail=detail)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_worktree_provisions_in_parallel(  # noqa: PLR0913 — each kwarg is a documented seam / test injection point.
    worktrees: Sequence[Worktree],
    *,
    executor: Callable[[Worktree], WorktreeProvisionResult],
    max_workers: int | None = None,
    admission_check: Callable[[], ProvisionAdmissionVerdict] | None = None,
    write: Callable[[str], object] | None = None,
    sleep: Callable[[float], object] = time.sleep,
    poll_interval: float = 2.0,
    max_hold_seconds: float = DEFAULT_MAX_ADMISSION_HOLD_SECONDS,
    now: Callable[[], float] = time.monotonic,
) -> list[WorktreeProvisionResult]:
    """Run *executor* for every worktree under a bounded, RAM-admitted pool.

    Results are returned in the SAME order as *worktrees*, regardless of
    completion order. When *admission_check* holds (RAM at/above the
    configured ceiling), the next-in-line worktree is NOT submitted — the loop
    re-checks admission on its next poll, so a held request drains
    automatically once RAM frees, without a separate durable queue. A hold
    that outlasts *max_hold_seconds* is overridden (logged loud) rather than
    blocking this CLI invocation forever — the "never hang silently" invariant
    applies to admission exactly as it does to a single provisioning step.
    """
    if not worktrees:
        return []
    workers = max_workers if max_workers is not None else resolve_provision_max_concurrency()
    admit = admission_check or check_provision_admission
    out = write or (lambda _msg: None)

    results: dict[int, WorktreeProvisionResult] = {}
    pending = list(worktrees)
    futures: dict[Future[WorktreeProvisionResult], Worktree] = {}
    held_since: float | None = None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending or futures:
            while pending and len(futures) < workers:
                verdict = admit()
                overridden = not verdict.ok and held_since is not None and (now() - held_since) > max_hold_seconds
                if not verdict.ok and not overridden:
                    held_since = held_since if held_since is not None else now()
                    out(f"  Holding {pending[0].repo_path} — {verdict.reason}")
                    break
                if overridden:
                    out(f"  Overriding hold on {pending[0].repo_path} after {max_hold_seconds:.0f}s — proceeding.")
                    logger.warning(
                        "provision admission held %r past %ss — overriding and proceeding",
                        pending[0].repo_path,
                        max_hold_seconds,
                    )
                held_since = None
                worktree = pending.pop(0)
                out(f"  Provisioning {worktree.repo_path}…")
                futures[pool.submit(executor, worktree)] = worktree

            if not futures:
                sleep(poll_interval)
                continue

            done, _pending_futures = wait(futures, timeout=poll_interval, return_when=FIRST_COMPLETED)
            for future in done:
                worktree = futures.pop(future)
                result = future.result()
                results[worktree.pk] = result
                out(f"    {result.detail}")

    return [results[worktree.pk] for worktree in worktrees]


def render_worktree_report(worktree: Worktree) -> str:
    """Render *worktree*'s persisted ``provision_report`` as a per-step table (``workspace provision --report``).

    A placeholder line (not an error) when the worktree was never
    provisioned under the instrumented runner — an absent key.
    """
    data = (worktree.extra or {}).get("provision_report")
    if not data:
        return f"  {worktree.repo_path}: no provision report recorded"
    report = ProvisionReport.from_dict(data)
    header = f"  ── {worktree.repo_path} ──"
    return f"{header}\n{report.summary()}"


__all__ = [
    "DEFAULT_WORKTREE_PROVISION_TIMEOUT_SECONDS",
    "WorktreeProvisionResult",
    "provision_worktree_subprocess",
    "render_worktree_report",
    "run_worktree_provisions_in_parallel",
]
