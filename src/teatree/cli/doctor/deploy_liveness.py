"""Is a deploy convergence in flight, provably gone, or unknowable from here (#4359)?

The stranded-``worker_quiescing`` detector clears a gate no deploy can still explain, and
that write resumes admission for the whole factory — so it is authorised by DEADNESS, never
by the gate's age alone. This probe answers in three values on the pattern
:func:`~teatree.core.worktree.probe_checkout` uses: a verdict it cannot establish is
``UNKNOWN``, and the caller reports instead of repairing.

Two signals, and neither is sufficient alone. ``deploy.sh`` holds a host flock for the whole
convergence, but ``/proc/locks`` is filtered by pid namespace, so from a container the lock
reads as free whether or not it is held — the flock cannot be probed from where the doctor
runs. What crosses that boundary is the deploy's own in-progress record (``<pid> <epoch>``
written into the lock FILE under the flock and cleared by its exit trap), which a crash loop
cannot forge. That record is stamped ONCE at the start, though, so a legal drain longer than
the ceiling ages it out while the deploy is very much alive; the host process table
(``/host-proc``, read through the one seam that knows whether it covers the host) is what
keeps a stale stamp from reading as a dead convergence.
"""

import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from teatree.core.cleanup.process_table import host_proc_root

#: The host's temp root as the containerised deployment mounts it (deploy/docker-compose.yml).
_HOST_TMP = Path("/host-tmp")
_VENUE_TMP = Path("/tmp")  # noqa: S108 — naming the deploy's own lock path, not creating a temp file
#: ``deploy.sh``'s own override and the file it defaults to.
_LOCK_ENV = "TEATREE_DEPLOY_LOCK"
_LOCK_NAME = "teatree-deploy.lock"
#: What a convergence's command line names. A false match reads LIVE, which only ever
#: withholds the repair — the safe direction for a substring.
_DEPLOY_SCRIPT = "deploy.sh"
_RECORD_FIELDS = 2


class DeployLiveness(Enum):
    """Whether a convergence can still explain what it left behind."""

    LIVE = "live"
    GONE = "gone"
    UNKNOWN = "unknown"


class _Record(Enum):
    """The deploy's in-progress record as this venue reads it."""

    FRESH = "fresh"
    RETIRED = "retired"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class DeployView:
    """The two artefacts a liveness verdict needs, as THIS venue can reach them.

    ``None`` on either side means this venue cannot see it — never that it is absent.
    """

    lock: Path | None
    proc_root: Path | None


def resolve_deploy_view() -> DeployView:
    """Where this venue can read the deploy's lock file and a host-covering process table."""
    configured = os.environ.get(_LOCK_ENV, "").strip()
    lock = Path(configured) if configured else (_HOST_TMP if _HOST_TMP.is_dir() else _VENUE_TMP) / _LOCK_NAME
    proc_root, _refusal = host_proc_root()
    return DeployView(lock=lock if lock.exists() else None, proc_root=proc_root)


def _read_deploy_record(lock: Path | None, *, max_age: float) -> _Record:
    if lock is None:
        return _Record.UNREADABLE
    try:
        lines = lock.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return _Record.UNREADABLE
    fields = lines[0].split() if lines else []
    if not fields:
        return _Record.RETIRED
    if len(fields) < _RECORD_FIELDS or not fields[1].isdigit():
        return _Record.UNREADABLE
    return _Record.FRESH if time.time() - int(fields[1]) < max_age else _Record.RETIRED


def _process_table_verdict(proc_root: Path) -> DeployLiveness:
    try:
        pids = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return DeployLiveness.UNKNOWN
    if not pids:
        return DeployLiveness.UNKNOWN
    for pid in pids:
        try:
            cmdline = (pid / "cmdline").read_bytes()
        except OSError:
            continue
        if _DEPLOY_SCRIPT in cmdline.decode("utf-8", errors="replace"):
            return DeployLiveness.LIVE
    return DeployLiveness.GONE


def probe_deploy_liveness(*, record_max_age: float, view: DeployView | None = None) -> DeployLiveness:
    """Whether a convergence is in flight, provably gone, or unknowable from this venue.

    ``record_max_age`` is the window in which the deploy's own start stamp still describes a
    plausibly-running convergence; the caller owns it because the caller owns the budget the
    deploy's stages sum to. ``GONE`` requires BOTH signals to answer — a retired record AND a
    host-covering process table listing no convergence.
    """
    resolved = resolve_deploy_view() if view is None else view
    record = _read_deploy_record(resolved.lock, max_age=record_max_age)
    if record is _Record.UNREADABLE:
        return DeployLiveness.UNKNOWN
    if record is _Record.FRESH:
        return DeployLiveness.LIVE
    if resolved.proc_root is None:
        return DeployLiveness.UNKNOWN
    return _process_table_verdict(resolved.proc_root)


__all__ = ["DeployLiveness", "DeployView", "probe_deploy_liveness", "resolve_deploy_view"]
