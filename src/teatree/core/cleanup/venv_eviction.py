"""Evict dormant virtualenvs — the reclaim that loses nothing (#4244).

A ``.venv`` is a build product of ``uv sync``: the source tree, the commits and
every uncommitted change live outside it, so removing one from a checkout nobody
is working in costs a re-sync and nothing else. That is what makes it the right
thing to reclaim on a full disk — measured at ~82 GB across two locations on the
box that produced this issue, against a pressure pass that could only ever return
~1.6 GB of docker cache.

**Both locations, because only one of them is in a ledger.** Roughly half the
checkouts carrying a venv are ad-hoc session checkouts that teatree never
registered, so a reaper keyed on the ``Worktree`` table walks past them. The
population here is the filesystem checkout scan, which is blind to whether
anything registered them.

**The guard is a live process, never a timestamp.** A venv is not written to
while it is being imported from, so an in-use one reads as idle by mtime — during
the measurement that produced this issue exactly one such checkout had a live
agent attached and would have had the floor pulled out from under it. Idleness
only ever narrows the candidate set; a live process is what decides.

**How much it narrows is a function of disk pressure (#4644).** A checkout some
other process rewrites hourly never ages past a fixed ``venv_idle_days``, so it
was ineligible on every pass forever however full the disk got. The caller
therefore scales the requirement with measured free space and passes ``None``
below the critical floor, where age stops gating and liveness is the sole guard —
:mod:`teatree.core.cleanup.reclaim_pressure` holds that policy.

That makes the process table load-bearing rather than advisory, so an unreadable
one refuses the whole pass (:mod:`teatree.core.cleanup.process_table` explains
the venue that produced it). This is stricter than
:mod:`teatree.core.cleanup.cleanup_liveness`, deliberately: that reaper proves a
worktree's every change redundant before wiping it and its CWD signal is one
guard among several, whereas here "no process is inside" IS the authority to
delete.

An enumeration gap does NOT refuse the pass, which is the opposite of the
env-dir reaper's rule and worth being explicit about: a checkout the scan missed
is simply never a candidate, so a gap costs reclaim rather than safety.

**The guard is re-established at the moment of deletion.** Planning and deleting
are separated by the enumeration walk, the venv sizing walks, the uv cache prune
and the docker reclaim — 34-68 s for the walk alone on the box that produced this
issue — and an agent that starts work inside a checkout in that window was, until
#4244, deleted out from under. So :func:`evict_venvs` re-reads the table and
re-runs :func:`_in_use_reason` per candidate.

It re-runs that half and NOT :func:`_dormancy_reason`, which is the same split
the guard doctrine above draws: liveness is the authority, idleness only narrows.
Re-judging idleness at deletion would also be self-defeating — removing one venv
rewrites its checkout's mtime, so a sibling venv in that checkout reads as
freshly touched and no pass ever reclaims it.
"""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from teatree.core.cleanup.checkout_registry import live_checkout_paths, one_spelling_each
from teatree.core.cleanup.process_table import ProcessTable, read_process_table

#: The virtualenv directory names teatree provisions into a checkout.
_VENV_NAMES = (".venv", ".venv-hook")

#: How many venvs one pass may evict. A bound on the blast radius, not a coverage
#: claim — what it defers is reported, and the eligible set is ranked by size
#: first so a capped pass returns the most bytes rather than the first 25 walked.
_MAX_EVICTIONS_PER_PASS = 25


@dataclass(frozen=True, slots=True)
class VenvCandidate:
    """One dormant virtualenv and what removing it would return."""

    venv: Path
    checkout: Path
    size_bytes: int


@dataclass(frozen=True, slots=True)
class VenvEvictionPlan:
    """What a pass would evict, what it kept, and why it could not see more."""

    candidates: tuple[VenvCandidate, ...] = ()
    kept: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    considered: int = 0
    refusal: str = ""

    @property
    def estimated_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class EvictionOutcome:
    """What an eviction actually did — what it freed, what the guard stopped, why it refused."""

    freed_bytes: int = 0
    skipped: tuple[str, ...] = ()
    refusal: str = ""


def plan_venv_eviction(workspace: Path, *, idle_days: float | None) -> VenvEvictionPlan:
    """Which venvs this pass may evict — empty with a ``refusal`` when it may not.

    ``idle_days=None`` means dormancy does not gate at all, which is what disk
    pressure below the critical floor buys (:mod:`teatree.core.cleanup.reclaim_pressure`).
    Liveness is unaffected by it.
    """
    table = read_process_table()
    if refusal := table.refuse_reason():
        return VenvEvictionPlan(refusal=refusal)
    registry = live_checkout_paths(workspace)
    cutoff = None if idle_days is None else timezone.now().timestamp() - idle_days * 86400
    eligible: list[VenvCandidate] = []
    kept: list[str] = []
    considered = 0
    for checkout in one_spelling_each(registry.paths):
        for venv in _venvs_in(checkout):
            considered += 1
            reason = _keep_reason(venv, checkout=checkout, table=table, cutoff=cutoff)
            if reason:
                kept.append(f"{venv}: {reason}")
            else:
                eligible.append(VenvCandidate(venv, checkout, _dir_size_bytes(venv)))
    ranked = sorted(eligible, key=lambda candidate: candidate.size_bytes, reverse=True)
    kept.extend(_deferred_lines(ranked[_MAX_EVICTIONS_PER_PASS:]))
    return VenvEvictionPlan(tuple(ranked[:_MAX_EVICTIONS_PER_PASS]), tuple(kept), registry.gaps, considered)


def _deferred_lines(deferred: list[VenvCandidate]) -> list[str]:
    """Name each venv the cap held back, then the total it left on the disk."""
    if not deferred:
        return []
    lines = [
        f"{candidate.venv}: over the {_MAX_EVICTIONS_PER_PASS}-per-pass cap, deferred to the next pass"
        for candidate in deferred
    ]
    total = sum(candidate.size_bytes for candidate in deferred)
    lines.append(f"deferred to the next pass: {len(deferred)} venv(s), {total} bytes")
    return lines


def evict_venvs(plan: VenvEvictionPlan) -> EvictionOutcome:
    """Remove every planned venv the guard still allows, re-judged at the moment of deletion."""
    table = read_process_table()
    if refusal := table.refuse_reason():
        return EvictionOutcome(refusal=f"the process table stopped answering after planning — {refusal}")
    freed = 0
    skipped: list[str] = []
    for candidate in plan.candidates:
        reason = _in_use_reason(candidate.venv, checkout=candidate.checkout, table=table)
        if reason:
            skipped.append(f"{candidate.venv}: {reason} since it was planned")
            continue
        try:
            shutil.rmtree(candidate.venv)
        except OSError:
            continue
        freed += candidate.size_bytes
    return EvictionOutcome(freed, tuple(skipped))


def _venvs_in(checkout: Path) -> list[Path]:
    return [venv for name in _VENV_NAMES if (venv := checkout / name).is_dir()]


def _keep_reason(venv: Path, *, checkout: Path, table: ProcessTable, cutoff: float | None) -> str:
    """Why *venv* survives this pass, or ``""`` when nothing keeps it."""
    if in_use := _in_use_reason(venv, checkout=checkout, table=table):
        return in_use
    if cutoff is None:
        return ""
    return _dormancy_reason(venv, checkout=checkout, cutoff=cutoff)


def _in_use_reason(venv: Path, *, checkout: Path, table: ProcessTable) -> str:
    """Somebody is using it — the half that AUTHORISES the delete, so the half re-run at deletion."""
    if _is_this_interpreters_venv(venv):
        return "this process is running from it"
    if table.holds(checkout):
        return "a live process is working inside the checkout"
    return ""


def _dormancy_reason(venv: Path, *, checkout: Path, cutoff: float) -> str:
    """It was touched too recently — a plan-time NARROWING, never the authority.

    Deliberately not re-run at deletion: removing one venv rewrites its
    checkout's mtime, so a second venv in the same checkout would read as
    freshly touched and never be reclaimed by any pass.
    """
    touched = _last_touched(venv, checkout)
    if touched is None:
        return "its age could not be read"
    if touched >= cutoff:
        return "touched too recently to be dormant"
    return ""


def _is_this_interpreters_venv(venv: Path) -> bool:
    active = [Path(sys.prefix)]
    if virtual_env := os.environ.get("VIRTUAL_ENV"):
        active.append(Path(virtual_env))
    return any(prefix == venv or venv in prefix.parents for prefix in active)


def _last_touched(venv: Path, checkout: Path) -> float | None:
    """The later of the venv's and its checkout's mtime — ``None`` when unreadable.

    The checkout is folded in because provisioning writes into it, which errs
    toward calling a settled checkout recent. Erring that way keeps a venv.
    """
    try:
        return max(venv.stat().st_mtime, checkout.stat().st_mtime)
    except OSError:
        return None


def _dir_size_bytes(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


__all__ = ["EvictionOutcome", "VenvCandidate", "VenvEvictionPlan", "evict_venvs", "plan_venv_eviction"]
