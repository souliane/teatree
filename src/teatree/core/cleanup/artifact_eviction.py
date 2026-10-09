"""Evict dormant build artifacts — the reclaim that loses nothing (#4244).

A ``.venv`` is a build product of ``uv sync``, a ``node_modules`` of ``npm ci``,
an ``.nx``/``.angular`` of the next build: the source tree, the commits and every
uncommitted change live outside them, so removing one from a checkout nobody is
working in costs a rebuild and nothing else. That is what makes them the right
thing to reclaim on a full disk — measured at ~82 GB of virtualenvs alone across
two locations on the box that produced this issue, against a pressure pass that
could only ever return ~1.6 GB of docker cache.

**Both locations, because only one of them is in a ledger.** Roughly half the
checkouts carrying an artifact are ad-hoc session checkouts that teatree never
registered, so a reaper keyed on the ``Worktree`` table walks past them. The
population here is the filesystem checkout scan, which is blind to whether
anything registered them.

**The guard is a live process, never a timestamp.** A venv is not written to
while it is being imported from, so an in-use one reads as idle by mtime — during
the measurement that produced this issue exactly one such checkout had a live
agent attached and would have had the floor pulled out from under it. Idleness
only ever narrows the candidate set; a live process is what decides.

**How much idleness narrows is a function of disk pressure (#4644).** A checkout some
other process rewrites hourly never ages past a fixed ``artifact_idle_days``, so it was
ineligible on every pass forever however full the disk got. The caller therefore scales
the requirement with measured free space and passes ``None`` below the critical floor,
where age stops gating and liveness is the sole guard —
:mod:`teatree.core.cleanup.reclaim_pressure` holds that policy.

That makes the process table load-bearing rather than advisory, so an unreadable
one refuses the whole pass (:mod:`teatree.core.cleanup.process_table` explains
the venue that produced it). This is stricter than
:mod:`teatree.core.cleanup.cleanup_liveness`, deliberately: that reaper proves a
worktree's every change redundant before wiping it and its CWD signal is one
guard among several, whereas here "no process is inside" IS the authority to
delete.

**Candidates and protectors are two populations** (:mod:`teatree.core.cleanup.venue_population`).
Candidates are the bounded checkouts under the roots this venue provisions into;
protectors are every checkout whose links could point at one — the candidates, every
worktree a clone registers wherever it lives, and the clones nested inside a candidate.
An unreadable protector or link is a gap, and a gap refuses the pass: a hidden link
can guard a shared target nothing else does.

**The guard is re-established before EACH deletion, without re-walking.** Up to
:data:`~teatree.core.cleanup.artifact_sizing.MAX_EVICTIONS_PER_PASS` deletions of
multi-GB trees separate the plan from the last delete, so the process table is re-read
and :class:`~teatree.core.cleanup.artifact_protection.ArtifactProtection` refreshed per
candidate — re-reading only the checkouts and registries whose mtime moved.

**A SYMLINKED artifact is not a candidate at all.** Overlays legitimately symlink
a worktree's ``node_modules``/``.venv`` at its main clone's, and every primitive
this module uses reads through such a link: ``is_dir()`` follows it, ``os.walk``
descends a symlinked top, and only ``shutil.rmtree``'s own refusal — an
``OSError`` this module used to swallow — kept the shared tree alive. Safety that
is accidental, undocumented and unreported is not safety. So a symlink is skipped
whole and reported: it is never sized (sizing one reports the clone's bytes and
promotes the wrong candidates under a size-ordered cap), never deleted, and never
even unlinked — the link is a provision artifact the overlay health-checks, and
removing it frees nothing because the bytes are in the clone.

**And "rebuildable" is CHECKED, not asserted.** The name set applies to every
``.git``-carrying directory under the scan roots, so it reaches scratch repos that
never had a lockfile — and ``npm ci`` refuses outright without one while ``uv sync``
needs a manifest. There the documented recovery cannot restore what was removed, which
is a different kind of loss from the one this pass claims to be free of, so
:func:`~teatree.core.cleanup.artifact_rebuild.unrebuildable_reason` keeps the artifact.
``.nx``/``.angular`` are exempt: a
build regenerates them unconditionally, with no manifest to consult.

**Nor is what a symlink POINTS AT.** Skipping the link protects the link; the
shared directory it resolves to is a real artifact at a checkout root, so every
other guard here waves it through. ``_in_use_reason`` asks about the sweeping
interpreter and the clone's own process placement — and a node process serving a
worktree has its cwd in the WORKTREE and its exe under a version manager, so
neither placement lands in the clone. ``_dormancy_reason`` reads only the clone's
own mtimes. And being the largest object on the box, the shared tree is promoted
FIRST by the size-ordered cap. Deleting it dangles every worktree's link at once,
which is the failure the overlay's symlink step exists to prevent, and recovery
needs an ``npm ci`` nothing triggers.

The guard is therefore STRUCTURAL rather than circumstantial: before anything is
planned, every protector's artifact symlinks are resolved and a candidate whose inode is
a target, or an ancestor of one, is excluded outright. It consults no mtime, no process
placement and no ordering — the three things that were incidentally keeping the shared
tree alive. The same reading is refreshed immediately before each deletion:
``workspace ticket`` creates a checkout and ``worktree provision`` symlinks its
artifacts, so a plan computed between those two steps would otherwise delete the tree
the next step points at.

A link this venue cannot RESOLVE is a gap, never an absent target: contributing
its unresolved spelling would leave the real target unprotected, which is the
wrong direction for a guard that authorises deletion.
"""

import dataclasses
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from teatree.core.cleanup.artifact_lock import (
    ARTIFACT_NAMES,
    artifact_lock_refusal_reason,
    artifact_name_pattern,
    artifact_source_lock,
)
from teatree.core.cleanup.artifact_protection import ArtifactProtection
from teatree.core.cleanup.artifact_rebuild import rebuild_inputs_for, unrebuildable_reason
from teatree.core.cleanup.artifact_removal import (
    EVICTED_PREFIX,
    AnchoredArtifact,
    clear_evicted,
    remove_anchored_artifact,
)
from teatree.core.cleanup.artifact_sizing import ArtifactCandidate, budget_spent, largest_first, path_identity
from teatree.core.cleanup.checkout_registry import one_spelling_each
from teatree.core.cleanup.process_table import ProcessTable, read_process_table
from teatree.core.cleanup.venue_population import venue_population

#: Rebuildable build products a checkout can lose without losing work — each one
#: root-level, hundreds of MB to GB, and restored by a single documented command
#: (``uv sync``, ``npm ci``, ``nx reset``).
#:
#: Deliberately NOT derived from ``checkout_registry._NEVER_A_CHECKOUT``. That set
#: answers "can this directory CONTAIN a checkout?" and therefore correctly
#: contains ``.git`` — the exact directory this set must never contain, since it
#: holds every commit, ref and stash. Two questions, two constants; sharing one
#: would let a locally-correct addition to the walk-skip list authorise deleting
#: repositories. ``__pycache__`` is excluded on cost/benefit: they are numerous,
#: a few KB each, nested at arbitrary depth rather than at the root this scan
#: looks at, and Python regenerates them on next import.
_ARTIFACT_NAMES = ARTIFACT_NAMES

_SYMLINK_REASON = (
    "a symlink into another checkout — the bytes live in the clone, and the link is a provisioned artifact"
)


_SHARED_TARGET_REASON = (
    "another checkout's artifact symlink resolves here — deleting it would dangle every link at once"
)

_EMPTY_POPULATION_REFUSAL = (
    "the plan names candidates but carries no protection reading, so the shared-target guard would be "
    "structurally disarmed — a plan is deletable only alongside the links it was computed over"
)

_BUDGET_REFUSAL = "the pass's time budget ran out"


@dataclass(frozen=True, slots=True)
class _Guards:
    """The three facts a delete is authorised by, resolved afresh before every deletion.

    Grouped because they travel together and are re-read together: what makes them one
    value is that none of them is a property of the candidate, so a snapshot of any one
    of them goes stale on the same clock.
    """

    table: ProcessTable
    protection: ArtifactProtection


@dataclass(frozen=True, slots=True)
class _CheckoutArtifacts:
    """What sits at one checkout's root: real artifacts, artifact links, and trees an earlier pass moved aside."""

    artifacts: tuple[Path, ...] = ()
    links: tuple[Path, ...] = ()
    leftovers: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactEvictionPlan:
    """What a pass would evict, what it kept, what it deferred, and why it could not see more."""

    candidates: tuple[ArtifactCandidate, ...] = ()
    #: Artifacts a SAFETY guard withheld. Kept apart from :attr:`deferred` because one
    #: says "this must never be deleted" and the other "not this pass", and a single
    #: count reported for both reads as a guard firing on a routine backlog.
    kept: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    considered: int = 0
    refusal: str = ""
    #: The protector links read at plan time, refreshed (never re-walked) before each deletion.
    protection: ArtifactProtection | None = None
    #: The ``time.monotonic()`` instant the whole pass — plan and deletions — must end by.
    deadline: float | None = None
    excluded: tuple[str, ...] = ()
    leftovers: tuple[Path, ...] = ()

    @property
    def estimated_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class EvictionOutcome:
    """What an eviction actually did — what it freed, what the guard stopped, why it refused."""

    freed_bytes: int = 0
    skipped: tuple[str, ...] = ()
    refusal: str = ""
    evicted: tuple[str, ...] = ()


def plan_artifact_eviction(
    workspace: Path, *, idle_days: float | None, budget_seconds: float | None = None
) -> ArtifactEvictionPlan:
    """Which dormant artifacts this pass may evict — empty with a ``refusal`` when it may not.

    ``idle_days=None`` means dormancy does not gate at all, which is what disk pressure
    below the critical floor buys (:mod:`teatree.core.cleanup.reclaim_pressure`). Liveness
    is unaffected by it. *budget_seconds* bounds the whole pass, deletions included: a walk
    it cuts short is a gap, and the sizing and the batch it cuts short are deferred.
    """
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    table = read_process_table()
    if refusal := table.refuse_reason():
        return ArtifactEvictionPlan(refusal=refusal)
    population = venue_population(workspace, deadline=deadline)
    classified = {checkout: _classify_artifacts_in(checkout) for checkout in one_spelling_each(population.candidates)}
    protection = ArtifactProtection.read(population.protectors, population.clones, population.listed)
    gaps = population.gaps + tuple(protection.gaps)
    base = ArtifactEvictionPlan(
        protection=protection,
        deadline=deadline,
        excluded=population.excluded,
        leftovers=tuple(leftover for found in classified.values() for leftover in found.leftovers),
    )
    if gaps:
        return dataclasses.replace(base, gaps=gaps, refusal=_enumeration_refusal(gaps))
    cutoff = None if idle_days is None else timezone.now().timestamp() - idle_days * 86400
    eligible, kept = _triage(classified, guards=_Guards(table=table, protection=protection), cutoff=cutoff)
    candidates, deferred = largest_first(eligible, deadline=deadline)
    return dataclasses.replace(
        base,
        candidates=candidates,
        kept=tuple(kept),
        deferred=tuple(deferred),
        gaps=gaps,
        considered=len(eligible) + len(kept),
    )


def _triage(
    classified: dict[Path, _CheckoutArtifacts], *, guards: _Guards, cutoff: float | None
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Each artifact is either eligible or kept with its reason; a symlinked one is always kept."""
    eligible: list[tuple[Path, Path]] = []
    kept: list[str] = []
    for checkout, found in classified.items():
        kept.extend(f"{link}: {_SYMLINK_REASON}" for link in found.links)
        for artifact in found.artifacts:
            if reason := _keep_reason(artifact, checkout=checkout, guards=guards, cutoff=cutoff):
                kept.append(f"{artifact}: {reason}")
            else:
                eligible.append((artifact, checkout))
    return eligible, kept


def evict_artifacts(plan: ArtifactEvictionPlan) -> EvictionOutcome:
    """Remove every planned artifact the guard still allows, re-judged before EACH deletion.

    The process table is re-read and the protector links refreshed PER CANDIDATE under the
    artifact lock, from the population the plan computed — a deletion never re-walks it. The
    batch stops, naming what it left, once the pass budget is spent.
    """
    freed, cleared = _clear_leftovers(plan)
    if not plan.candidates:
        return EvictionOutcome(freed_bytes=freed, evicted=cleared)
    if plan.protection is None:
        return EvictionOutcome(freed_bytes=freed, evicted=cleared, refusal=_EMPTY_POPULATION_REFUSAL)
    skipped: list[str] = []
    evicted = list(cleared)
    for index, candidate in enumerate(plan.candidates):
        if budget_spent(plan.deadline):
            partial = EvictionOutcome(freed_bytes=freed, skipped=tuple(skipped), evicted=tuple(evicted))
            return _stopped_outcome(plan, index, partial, _BUDGET_REFUSAL)
        with artifact_source_lock(candidate.artifact, blocking=False) as locked:
            if not locked:
                skipped.append(f"{candidate.artifact}: {artifact_lock_refusal_reason(candidate.artifact)}")
                continue
            guards = _fresh_guards(plan.protection)
            if isinstance(guards, str):
                partial = EvictionOutcome(freed_bytes=freed, skipped=tuple(skipped), evicted=tuple(evicted))
                return _stopped_outcome(plan, index, partial, guards)
            if reason := _delete_time_reason(candidate, guards=guards):
                skipped.append(f"{candidate.artifact}: {reason} since it was planned")
                continue
            if reason := _remove_anchored_candidate(candidate, deadline=plan.deadline):
                skipped.append(f"{candidate.artifact}: {reason}")
                continue
            freed += candidate.size_bytes
            evicted.append(str(candidate.artifact))
    return EvictionOutcome(freed_bytes=freed, skipped=tuple(skipped), evicted=tuple(evicted))


def _clear_leftovers(plan: ArtifactEvictionPlan) -> tuple[int, tuple[str, ...]]:
    """Finish the trees an earlier pass moved aside; they left their path already, so no guard applies."""
    freed = 0
    cleared: list[str] = []
    for leftover in plan.leftovers:
        try:
            complete, bytes_freed = clear_evicted(leftover.parent, leftover.name, deadline=plan.deadline)
        except OSError:
            continue
        freed += bytes_freed
        if complete:
            cleared.append(str(leftover))
    return freed, tuple(cleared)


def _fresh_guards(protection: ArtifactProtection) -> _Guards | str:
    """The guards re-read for the next deletion, or why the batch must stop instead."""
    if gaps := protection.refresh():
        return _enumeration_refusal(gaps)
    table = read_process_table()
    if refusal := table.refuse_reason():
        return f"the process table stopped answering mid-batch — {refusal}"
    return _Guards(table=table, protection=protection)


def _stopped_outcome(
    plan: ArtifactEvictionPlan,
    index: int,
    partial: EvictionOutcome,
    refusal: str,
) -> EvictionOutcome:
    left = plan.candidates[index:]
    stopped = [f"{candidate.artifact}: batch stopped before deletion — {refusal}" for candidate in left]
    return EvictionOutcome(
        freed_bytes=partial.freed_bytes,
        skipped=(*partial.skipped, *stopped),
        refusal=f"{refusal}; {len(left)} candidate(s) left untouched",
        evicted=partial.evicted,
    )


def _enumeration_refusal(gaps: tuple[str, ...]) -> str:
    return f"the checkout population is incomplete — {'; '.join(gaps)}"


def _classify_artifacts_in(checkout: Path) -> _CheckoutArtifacts:
    """The artifacts at *checkout*'s root, split real-vs-link, from ONE listing.

    ``is_dir()`` FOLLOWS symlinks, so it cannot be the only predicate: an
    overlay-provisioned ``node_modules`` link reads as a directory and would be
    selected, sized through the link, and aimed at a tree every worktree shares. An
    unreadable checkout yields nothing here; the protection reading records it as a gap.
    """
    artifacts: list[Path] = []
    links: list[Path] = []
    leftovers: list[Path] = []
    try:
        with os.scandir(checkout) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.name.startswith(EVICTED_PREFIX) and entry.is_dir(follow_symlinks=False):
                    leftovers.append(path)
                elif artifact_name_pattern(entry.name) is None:
                    continue
                elif entry.is_symlink():
                    links.append(path)
                elif entry.is_dir():
                    artifacts.append(path)
    except OSError:
        return _CheckoutArtifacts()
    return _CheckoutArtifacts(tuple(sorted(artifacts)), tuple(sorted(links)), tuple(sorted(leftovers)))


def _keep_reason(artifact: Path, *, checkout: Path, guards: _Guards, cutoff: float | None) -> str:
    """Why *artifact* survives this pass, or ``""`` when nothing keeps it."""
    blocking = _authorisation_reason(artifact, checkout=checkout, guards=guards) or unrebuildable_reason(
        artifact, checkout=checkout
    )
    if blocking or cutoff is None:
        return blocking
    return _dormancy_reason(artifact, checkout=checkout, cutoff=cutoff)


def _delete_time_reason(candidate: ArtifactCandidate, *, guards: _Guards) -> str:
    """Why the guard stops this delete NOW — re-read before each delete, never inherited.

    Symlink status and the shared-target set are re-judged alongside liveness because
    provisioning replaces a real directory with a link into the main clone — exactly
    what the overlay's ``node_modules`` step does — and provisioning a NEW worktree
    aims a fresh link at an existing clone directory. Both land inside the batch's own
    delete loop, and both are decided by a fact outside this candidate.
    """
    if candidate.artifact.is_symlink():
        return "it became a symlink"
    return (
        _identity_change_reason(candidate)
        or unrebuildable_reason(candidate.artifact, checkout=candidate.checkout)
        or _authorisation_reason(candidate.artifact, checkout=candidate.checkout, guards=guards)
    )


def _remove_anchored_candidate(candidate: ArtifactCandidate, *, deadline: float | None = None) -> str:
    return remove_anchored_artifact(
        AnchoredArtifact(
            artifact=candidate.artifact,
            checkout=candidate.checkout,
            artifact_identity=candidate.artifact_identity,
            checkout_identity=candidate.checkout_identity,
            rebuild_inputs=rebuild_inputs_for(candidate.artifact),
        ),
        deadline=deadline,
    )


def _identity_change_reason(candidate: ArtifactCandidate) -> str:
    if candidate.checkout_identity is None or path_identity(candidate.checkout) != candidate.checkout_identity:
        return "the checkout identity changed"
    if candidate.artifact_identity is None or path_identity(candidate.artifact) != candidate.artifact_identity:
        return "the artifact identity changed"
    return ""


def _authorisation_reason(artifact: Path, *, checkout: Path, guards: _Guards) -> str:
    """The half of the verdict that AUTHORISES a delete, so the half re-run before each one.

    Rebuildability and dormancy are properties of the artifact that a deletion elsewhere
    cannot change; these three are properties of the world, and the world moves during a
    batch.
    """
    return _shared_target_reason(artifact, protection=guards.protection) or _in_use_reason(
        artifact, checkout=checkout, table=guards.table
    )


def _shared_target_reason(artifact: Path, *, protection: ArtifactProtection) -> str:
    """A protector's artifact symlink resolves here or below — the STRUCTURAL half of the guard.

    An artifact whose own identity cannot be read is KEPT: the comparison is by inode, so
    an unread one can only ever fail to match.
    """
    try:
        status = artifact.stat()
    except OSError as exc:
        return f"its identity could not be read ({exc}), so a link resolving here cannot be ruled out"
    if protection.protects((status.st_dev, status.st_ino)):
        return _SHARED_TARGET_REASON
    return ""


def _in_use_reason(artifact: Path, *, checkout: Path, table: ProcessTable) -> str:
    """Somebody is using it — the half that AUTHORISES the delete, so the half re-run at deletion."""
    if _is_this_interpreters_venv(artifact):
        return "this process is running from it"
    if table.holds(checkout):
        return "a live process is working inside the checkout"
    return ""


def _dormancy_reason(artifact: Path, *, checkout: Path, cutoff: float) -> str:
    """It was touched too recently — a plan-time NARROWING, never the authority.

    Deliberately not re-run at deletion: removing one artifact rewrites its
    checkout's mtime, so a second artifact in the same checkout would read as
    freshly touched and never be reclaimed by any pass.
    """
    touched = _last_touched(artifact, checkout)
    if touched is None:
        return "its age could not be read"
    if touched >= cutoff:
        return "touched too recently to be dormant"
    return ""


def _is_this_interpreters_venv(artifact: Path) -> bool:
    active = [Path(sys.prefix)]
    if virtual_env := os.environ.get("VIRTUAL_ENV"):
        active.append(Path(virtual_env))
    return any(prefix == artifact or artifact in prefix.parents for prefix in active)


def _last_touched(artifact: Path, checkout: Path) -> float | None:
    """The later of the artifact's and its checkout's mtime — ``None`` when unreadable.

    The checkout is folded in because provisioning writes into it, which errs
    toward calling a settled checkout recent. Erring that way keeps an artifact.
    """
    try:
        return max(artifact.stat().st_mtime, checkout.stat().st_mtime)
    except OSError:
        return None


__all__ = [
    "ArtifactCandidate",
    "ArtifactEvictionPlan",
    "EvictionOutcome",
    "evict_artifacts",
    "plan_artifact_eviction",
]
