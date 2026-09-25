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

**An enumeration gap costs SAFETY once a guard reads the whole population.** While
every guard read the candidate's own checkout, a missed checkout was simply never
a candidate and a gap only cost reclaim. :func:`_symlink_targets` broke that: it
builds the protected set from the links THAT SAME SCAN found, so an unreadable
region hides links, and a shared target whose linking worktrees all sit in that
region is deleted, dangling every link at once. Gaps are not hypothetical —
:func:`~teatree.core.cleanup.checkout_registry.checkout_scan_roots` includes
``Path.home()`` unconditionally and macOS TCC refuses several ``~/Library``
subtrees, measured at 25 gaps on the box that produced this issue.

**The guard is re-established before EACH deletion.** Planning and deleting are
separated by the enumeration walk, the sizing walks, the uv cache prune and the
docker reclaim — 34-68 s for the walk alone on the box that produced this issue.
The delete LOOP is longer still: up to
:data:`_MAX_EVICTIONS_PER_PASS` ``rmtree`` calls over trees reaching tens of GB,
so a snapshot taken once at the top of :func:`evict_artifacts` is staler by the
last candidate than the plan-time snapshot #4244 replaced. So the table and the
checkout population and symlink-target set are re-read per candidate, not per batch.

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
planned, :func:`_symlink_targets` resolves every artifact symlink in the whole
scan population, and a candidate resolving into that set is excluded outright. It
consults no mtime, no process placement and no ordering — the three things that
were incidentally keeping the shared tree alive. (Measured on the box that
produced this: a frontend clone's ``.nx`` was 20 days dormant and survived only
because ``_last_touched`` folds in the checkout mtime, a fold whose stated reason
is provisioning writes, not shared targets.) Like liveness, and for the same
reason, the set is re-resolved over that population immediately before each
deletion: ``workspace ticket`` creates a checkout and ``worktree provision``
symlinks its artifacts, so a plan computed between those two steps — or a batch
still walking its way down its candidate list — sees a checkout carrying no link
yet and would delete the tree the next step points at.

A link this venue cannot RESOLVE is a gap, never an absent target: contributing
its unresolved spelling would leave the real target unprotected, which is the
wrong direction for a guard that authorises deletion.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from teatree.core.cleanup.artifact_lock import ARTIFACT_NAMES, artifact_lock_refusal_reason, artifact_source_lock
from teatree.core.cleanup.artifact_rebuild import rebuild_inputs_for, unrebuildable_reason
from teatree.core.cleanup.artifact_removal import remove_anchored_artifact
from teatree.core.cleanup.checkout_registry import live_checkout_paths, one_spelling_each
from teatree.core.cleanup.process_table import ProcessTable, read_process_table

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

#: How many artifacts one pass may evict. A bound on the blast radius, not a
#: coverage claim — the count dropped is reported. Raised with the name set, which
#: went from two names to five.
_MAX_EVICTIONS_PER_PASS = 50

#: How many eligible artifacts one pass may SIZE. Sizing is an ``os.walk`` per
#: candidate over trees reaching 100k inodes, and it runs INLINE in the tick beside
#: the pressure measurement and the intake sizing on the same mini-loop. Ordering the
#: whole eligible set by size therefore paid for a walk of every candidate it was
#: going to defer as well — on a box with a few hundred checkouts up to ~1200 walks per pass, with the
#: remainder re-walked on every pass until the backlog drained. The prefix is
#: alphabetical, NOT a sample of the biggest — on a box with more eligible artifacts than
#: this bound, the largest one is likely outside it and waits for a later pass. What makes
#: that acceptable is that the ordering is deterministic and the backlog drains: what a
#: pass evicts leaves the eligible set, so the next pass reaches further in.
_MAX_SIZED_PER_PASS = 200

_SYMLINK_REASON = (
    "a symlink into another checkout — the bytes live in the clone, and the link is a provisioned artifact"
)


_SHARED_TARGET_REASON = (
    "another checkout's artifact symlink resolves here — deleting it would dangle every link at once"
)

_EMPTY_POPULATION_REFUSAL = (
    "the plan names candidates but carries no population, so the shared-target guard would be "
    "structurally disarmed — a plan is deletable only alongside the checkouts it was computed over"
)


@dataclass(frozen=True, slots=True)
class ArtifactCandidate:
    """One dormant build artifact and what removing it would return."""

    artifact: Path
    checkout: Path
    size_bytes: int
    artifact_identity: tuple[int, int, int] | None = None
    checkout_identity: tuple[int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class _SharedTargets:
    """Where the population's artifact symlinks resolve, and which of them would not resolve.

    An unresolvable link is carried rather than dropped: it names a target this venue
    cannot see, so the deletion half must treat the population as incomplete instead of
    concluding no link points anywhere.
    """

    paths: frozenset[Path] = frozenset()
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Guards:
    """The three facts a delete is authorised by, resolved afresh before every deletion.

    Grouped because they travel together and are re-read together: what makes them one
    value is that none of them is a property of the candidate, so a snapshot of any one
    of them goes stale on the same clock.
    """

    table: ProcessTable
    shared: _SharedTargets


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
    #: The population this plan was computed over, carried so the deletion pass can
    #: re-resolve the symlink-target set rather than trust a snapshot taken before
    #: minutes of walks — the same reason liveness is re-established there.
    checkouts: tuple[Path, ...] = ()
    workspace: Path | None = None

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


def plan_artifact_eviction(workspace: Path, *, idle_days: float | None) -> ArtifactEvictionPlan:
    """Which dormant artifacts this pass may evict — empty with a ``refusal`` when it may not.

    ``idle_days=None`` means dormancy does not gate at all, which is what disk pressure
    below the critical floor buys (:mod:`teatree.core.cleanup.reclaim_pressure`). Liveness
    is unaffected by it.
    """
    table = read_process_table()
    if refusal := table.refuse_reason():
        return ArtifactEvictionPlan(refusal=refusal)
    registry = live_checkout_paths(workspace)
    checkouts = tuple(one_spelling_each(registry.paths))
    classified = _classify_population(checkouts)
    shared = _symlink_targets(classified)
    gaps = registry.gaps + shared.unresolved
    if gaps:
        return ArtifactEvictionPlan(
            gaps=gaps,
            refusal=_enumeration_refusal(gaps),
            checkouts=checkouts,
            workspace=workspace,
        )
    guards = _Guards(table=table, shared=shared)
    cutoff = None if idle_days is None else timezone.now().timestamp() - idle_days * 86400
    eligible: list[tuple[Path, Path]] = []
    kept: list[str] = []
    considered = 0
    for checkout, (artifacts, links) in classified.items():
        for link in links:
            considered += 1
            kept.append(f"{link}: {_SYMLINK_REASON}")
        for artifact in artifacts:
            considered += 1
            reason = _keep_reason(artifact, checkout=checkout, guards=guards, cutoff=cutoff)
            if reason:
                kept.append(f"{artifact}: {reason}")
            else:
                eligible.append((artifact, checkout))
    candidates, deferred = _largest_first(eligible)
    return ArtifactEvictionPlan(
        candidates=candidates,
        kept=tuple(kept),
        deferred=tuple(deferred),
        gaps=gaps,
        considered=considered,
        checkouts=checkouts,
        workspace=workspace,
    )


def evict_artifacts(plan: ArtifactEvictionPlan) -> EvictionOutcome:
    """Remove every planned artifact the guard still allows, re-judged before EACH deletion.

    The table and the symlink-target set are re-read PER CANDIDATE under the artifact
    lock. A batch is up to :data:`_MAX_EVICTIONS_PER_PASS` ``rmtree`` calls over trees
    reaching tens of GB, so one snapshot at the top is staler by the last candidate
    than the plan-time snapshot #4244 replaced — an agent that starts work, or a
    ``worktree provision`` that plants a link, between two deletions would be judged
    against a reading taken minutes earlier.
    """
    if not plan.candidates:
        return EvictionOutcome()
    if not plan.checkouts or plan.workspace is None:
        return EvictionOutcome(refusal=_EMPTY_POPULATION_REFUSAL)
    workspace = plan.workspace
    freed = 0
    skipped: list[str] = []
    evicted: list[str] = []
    for index, candidate in enumerate(plan.candidates):
        with artifact_source_lock(candidate.artifact, blocking=False) as locked:
            if not locked:
                skipped.append(f"{candidate.artifact}: {artifact_lock_refusal_reason(candidate.artifact)}")
                continue
            registry = live_checkout_paths(workspace)
            checkouts = tuple(one_spelling_each(registry.paths))
            shared = _symlink_targets(_classify_population(checkouts))
            gaps = registry.gaps + shared.unresolved
            if gaps:
                refusal = _enumeration_refusal(gaps)
                partial = EvictionOutcome(freed_bytes=freed, skipped=tuple(skipped), evicted=tuple(evicted))
                return _stopped_outcome(plan, index, partial, refusal)
            table = read_process_table()
            if refusal := table.refuse_reason():
                partial = EvictionOutcome(freed_bytes=freed, skipped=tuple(skipped), evicted=tuple(evicted))
                return _stopped_outcome(
                    plan,
                    index,
                    partial,
                    f"the process table stopped answering mid-batch — {refusal}",
                )
            guards = _Guards(table=table, shared=shared)
            if reason := _delete_time_reason(candidate, guards=guards):
                skipped.append(f"{candidate.artifact}: {reason} since it was planned")
                continue
            if reason := _remove_anchored_candidate(candidate):
                skipped.append(f"{candidate.artifact}: {reason}")
                continue
            freed += candidate.size_bytes
            evicted.append(str(candidate.artifact))
    return EvictionOutcome(freed_bytes=freed, skipped=tuple(skipped), evicted=tuple(evicted))


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


def _classify_population(checkouts: tuple[Path, ...]) -> dict[Path, tuple[list[Path], list[Path]]]:
    """Every checkout's artifacts, split into real directories and links, in one pass."""
    return {checkout: _classify_artifacts_in(checkout) for checkout in checkouts}


def _classify_artifacts_in(checkout: Path) -> tuple[list[Path], list[Path]]:
    """The artifact names at *checkout*'s root, split real-vs-link with ONE stat each.

    Two questions with one answer, because they are one classification: asking them
    separately re-stats the same five paths per checkout, which across the hundreds of
    checkouts on a real box is the same syscalls paid twice for the same fact.

    ``Path.is_dir()`` FOLLOWS symlinks, so it cannot be the only predicate: an
    overlay-provisioned ``node_modules`` link reads as a directory and would be
    selected, sized through the link, and aimed at a tree every worktree shares. The
    links are not merely excluded — they are RETURNED, because where they point is
    what :func:`_symlink_targets` must protect.
    """
    artifacts: list[Path] = []
    links: list[Path] = []
    for path in _artifact_paths_in(checkout):
        if path.is_symlink():
            links.append(path)
        elif path.is_dir():
            artifacts.append(path)
    return artifacts, links


def _artifact_paths_in(checkout: Path) -> list[Path]:
    """Every path at *checkout*'s root an :data:`_ARTIFACT_NAMES` entry names.

    A glob entry is EXPANDED rather than joined: ``checkout / ".venv-hook*"`` is a path
    nothing ever created, so joining it finds no platform's hook environment and reports
    the checkout as carrying none.
    """
    paths: list[Path] = []
    for name in _ARTIFACT_NAMES:
        paths.extend(sorted(checkout.glob(name)) if "*" in name else [checkout / name])
    return paths


def _symlink_targets(classified: dict[Path, tuple[list[Path], list[Path]]]) -> _SharedTargets:
    """Where every artifact symlink in the population resolves — the set no pass may delete.

    Collected across the WHOLE population before anything is planned, because the link
    and its target sit in different checkouts: the checkout holding the target has no
    local evidence that anyone depends on it.
    """
    targets: set[Path] = set()
    unresolved: list[str] = []
    for _artifacts, links in classified.values():
        for link in links:
            try:
                targets.add(link.resolve())
            except OSError as exc:
                unresolved.append(f"could not resolve the artifact link {link} ({exc})")
    return _SharedTargets(frozenset(targets), tuple(unresolved))


def _largest_first(eligible: list[tuple[Path, Path]]) -> tuple[tuple[ArtifactCandidate, ...], list[str]]:
    """Spend the cap's budget on the biggest of a BOUNDED prefix, and say what that deferred.

    First-come selection was near-enough arbitrary over two names per checkout; over five
    names across hundreds of checkouts it lets one pass burn its whole budget on kilobyte
    candidates while the multi-gigabyte ones are deferred forever. So the accepted set is
    size-ordered — but ordering the WHOLE eligible set means sizing every candidate the
    pass is about to defer, and sizing is the expensive part (see
    :data:`_MAX_SIZED_PER_PASS`).

    The prefix is ALPHABETICAL, so "the biggest" means the biggest of that prefix rather
    than of the box: past the sizing bound the largest artifact waits for a later pass.
    Determinism is what makes that drain rather than churn — the same prefix is sized each
    pass, the evicted ones leave the eligible set, and the next pass reaches further in.
    """
    ordered = sorted(eligible)
    measurable, unsized = ordered[:_MAX_SIZED_PER_PASS], ordered[_MAX_SIZED_PER_PASS:]
    sized = sorted(
        (
            ArtifactCandidate(
                artifact,
                checkout,
                _dir_size_bytes(artifact),
                _path_identity(artifact),
                _path_identity(checkout),
            )
            for artifact, checkout in measurable
        ),
        key=lambda candidate: candidate.size_bytes,
        reverse=True,
    )
    deferred = [
        f"{candidate.artifact}: over the {_MAX_EVICTIONS_PER_PASS}-per-pass cap, deferred to the next pass"
        for candidate in sized[_MAX_EVICTIONS_PER_PASS:]
    ]
    deferred += [
        f"{artifact}: over the {_MAX_SIZED_PER_PASS}-per-pass sizing bound, not measured this pass"
        for artifact, _checkout in unsized
    ]
    return tuple(sized[:_MAX_EVICTIONS_PER_PASS]), deferred


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


def _path_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        value = path.lstat()
    except OSError:
        return None
    return value.st_dev, value.st_ino, value.st_mode


def _remove_anchored_candidate(candidate: ArtifactCandidate) -> str:
    return remove_anchored_artifact(
        artifact=candidate.artifact,
        checkout=candidate.checkout,
        artifact_identity=candidate.artifact_identity,
        checkout_identity=candidate.checkout_identity,
        rebuild_inputs=rebuild_inputs_for(candidate.artifact),
    )


def _identity_change_reason(candidate: ArtifactCandidate) -> str:
    if candidate.checkout_identity is None or _path_identity(candidate.checkout) != candidate.checkout_identity:
        return "the checkout identity changed"
    if candidate.artifact_identity is None or _path_identity(candidate.artifact) != candidate.artifact_identity:
        return "the artifact identity changed"
    return ""


def _authorisation_reason(artifact: Path, *, checkout: Path, guards: _Guards) -> str:
    """The half of the verdict that AUTHORISES a delete, so the half re-run before each one.

    Rebuildability and dormancy are properties of the artifact that a deletion elsewhere
    cannot change; these three are properties of the world, and the world moves during a
    batch.
    """
    return _shared_target_reason(artifact, shared=guards.shared) or _in_use_reason(
        artifact, checkout=checkout, table=guards.table
    )


def _shared_target_reason(artifact: Path, *, shared: _SharedTargets) -> str:
    """Another checkout's artifact symlink resolves here — the STRUCTURAL half of the guard.

    An ancestor counts as well as an exact match: a link aimed at ``…/node_modules/x``
    is broken just as thoroughly by removing ``…/node_modules``. An artifact whose own
    real path cannot be read is KEPT: the comparison is between resolved paths, so an
    unresolved one can only ever fail to match.
    """
    try:
        resolved = artifact.resolve()
    except OSError as exc:
        return f"its real path could not be read ({exc}), so a link resolving here cannot be ruled out"
    if any(target == resolved or resolved in target.parents for target in shared.paths):
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


def _dir_size_bytes(directory: Path) -> int:
    """Bytes this tree owns — never a byte reached through a link out of it.

    ``followlinks=False`` is the default and is stated so the next reader does not
    "simplify" it away: a ``node_modules`` is full of internal links (``.bin/*``,
    workspace links) whose targets sit outside the tree being sized. A link's own
    inode is not credited either, so the figure is what deleting the tree returns.
    """
    total = 0
    for root, _, files in os.walk(directory, followlinks=False):
        for name in files:
            entry = Path(root) / name
            try:
                if not entry.is_symlink():
                    total += entry.lstat().st_size
            except OSError:
                continue
    return total


__all__ = [
    "ArtifactCandidate",
    "ArtifactEvictionPlan",
    "EvictionOutcome",
    "evict_artifacts",
    "plan_artifact_eviction",
]
