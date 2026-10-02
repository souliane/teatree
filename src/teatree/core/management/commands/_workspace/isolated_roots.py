"""Orphan auto-isolated worktree env-dir reaping for ``t3 teatree workspace clean-all``.

Its own module so :mod:`teatree.core.management.commands._workspace.cleanup`
stays under the module-health LOC + function caps (mirrors
``_workspace_docker``). A git worktree's auto-isolated env dir
(``~/.local/share/teatree-worktrees/<slug>`` holding a per-worktree
``db.sqlite3`` + ``logs/``) lingers after the checkout is gone; this reaps the
dirs no live checkout owns, never one holding a git checkout (#291, mirroring
the #706/#835 data-loss discipline).

**"Owned by a live checkout" has one answer here, fed by three evidence sources
(#3852).** It used to have one source — the ``Worktree`` table — while
:func:`teatree.paths.resolve_data_dir` mints an env dir for ANY checkout,
registered or not. The two ends of one deterministic slug mapping thus disagreed
about the population: on the host that produced this ticket, 13 rows against 169
dirs, so 79 dirs behind live checkouts were reported as orphans and ``clean-all``
became a command nobody could safely run. The keep-set now unions every checkout
found ON DISK (the primary source), every checkout git reports, and the
``Worktree`` rows — via ``checkout_registry.live_checkout_paths``, the same seam
the raw-orphan reaper asks — plus each dir's own owner stamp
(:class:`teatree.paths.IsolatedEnvDir`), which inverts the one-way slug hash so
liveness is PROVEN by the named path rather than inferred from a population the
other sources have to agree on. Each pass stamps what it discovered, so that
durable mapping grows rather than covering only newly-minted dirs.

Incomplete evidence fails CLOSED — an unreadable directory, a subtree past the
walk's depth cap, or an unreadable clone registry hides an unknown number of live
checkouts, so every otherwise-unreferenced dir is kept and the gap reported. That
rests on the scan reporting everything it did not cover: a skip recording no gap
left ``complete`` true while the keep-set was short, which is what authorised
proposing a live checkout's env dir for deletion (#3872). A dir modified at or
after the keep-set instant is kept for the same reason: the evidence never
covered it.

**A scan that skipped nothing can still be blind, so its silence is not evidence
either (#3872).** Making every skip record a gap closes the case where the walk
declined to look; it cannot close the case where there was nothing there to look
at. In the container the project mandates ``t3`` runs in, the host's clone is not
mounted: the walk reads every root that exists, skips nothing, records no gap, and
reports ``complete`` — while every host-owned env dir reads as an orphan. The
blindness is bidirectional (the host cannot see the container's own source volume
either), so no venue's scan result is a sound liveness test on its own. Only the
stamp is venue-independent, and
:mod:`~teatree.core.management.commands._workspace.owner_stamps` is where a
stamped owner's absence is weighed against whether this venue could have observed
it: a stamp naming an unreachable path, and a dir carrying no stamp at all, are
both MISSING EVIDENCE — kept with the gap reported, never proof of death.
"""

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from teatree import paths
from teatree.core.cleanup import checkout_registry
from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.gates.idle_stack import worktree_protects_against_reap
from teatree.core.management.commands._workspace import owner_stamps
from teatree.core.management.commands._workspace.preview import preview_line
from teatree.core.models import Worktree


def _has_unmappable_live_worktree() -> bool:
    """True iff a live worktree row lacks a recorded checkout path (#291 data-loss).

    A live worktree WITH a checkout path already contributes its slug to the
    keep-set (:func:`_live_checkout_slugs`), so its env dir is kept. But a
    live worktree whose canonical row LOST its ``worktree_path`` (the stale-row
    class the resolver tolerates) cannot be hashed to a slug, so its in-use
    isolated DB looks like an orphan. When any such row exists, no unreferenced
    dir can be proven dead — fail safe and keep them all rather than reap a live
    isolated DB out from under a mid-task agent.

    "Live" is the shared :func:`worktree_protects_against_reap` predicate — a live
    session, an active/claimed task, an external-delivery lease, a recent E2E run,
    or an explicit pin — so this reaper never protects LESS than the reversible
    idle-stack reaper.
    """
    for worktree in Worktree.objects.select_related("ticket"):
        extra = worktree.extra if isinstance(worktree.extra, dict) else {}
        if not str(extra.get("worktree_path", "")) and worktree_protects_against_reap(worktree) is not None:
            return True
    return False


@dataclass(frozen=True, slots=True)
class LiveCheckoutSlugs:
    """The env-dir slugs a live checkout owns, and any gap in the evidence behind them.

    ``gaps`` is the fail-closed channel. A slug's absence from ``slugs`` only means
    "dead" when the evidence was complete; with a clone unread it means "unknown",
    and unknown must never authorise a deletion.
    """

    slugs: frozenset[str]
    gaps: tuple[str, ...]
    #: When the evidence was gathered. A dir modified at or after this instant was
    #: not covered by it, so the snapshot says nothing about whether it is dead.
    snapshot_at: float
    #: The roots this venue actually walked — what a stamped owner's absence is
    #: weighed against, so "not found" and "not looked for here" stay distinct.
    scanned_roots: tuple[Path, ...]


def _row_referenced_slugs() -> set[str]:
    """Slugs of the auto-isolated env dirs owned by a registered ``Worktree`` row.

    A worktree's env dir — the dir holding its DB-backed isolated sqlite DB — is
    named by :func:`paths.isolated_slug` of its on-disk checkout path
    (``extra['worktree_path']``), the same deterministic mapping the resolver
    (:func:`paths.resolve_data_dir`) uses. A row with no recorded checkout path
    contributes nothing — its dir, if any, is unmappable, which is what
    :func:`_has_unmappable_live_worktree` exists to catch.
    """
    referenced: set[str] = set()
    for worktree in Worktree.objects.all():
        extra = worktree.extra if isinstance(worktree.extra, dict) else {}
        checkout = str(extra.get("worktree_path", ""))
        if checkout:
            referenced.add(paths.isolated_slug(Path(checkout)))
    return referenced


def _live_checkout_slugs(workspace: Path, root: Path) -> LiveCheckoutSlugs:
    """Union the registered-row and on-disk-checkout evidence into ONE keep-set.

    Both sources answer the same question — "does a live checkout own this slug?"
    — so they are unioned rather than consulted in turn: a checkout is live if
    EITHER knows it, and neither is authoritative alone (rows miss the checkouts
    nobody registered; the scan misses a checkout outside every scanned root).

    ``snapshot_at`` is taken BEFORE the evidence is gathered, so a dir modified
    at or after it is provably outside this answer.
    """
    snapshot_at = time.time()
    registry = checkout_registry.live_checkout_paths(workspace)
    owner_stamps.stamp_discovered_owners(registry.paths, root)
    slugs = _row_referenced_slugs()
    slugs.update(paths.isolated_slug(Path(checkout)) for checkout in registry.paths)
    return LiveCheckoutSlugs(frozenset(slugs), registry.gaps, snapshot_at, registry.scanned_roots)


def _holds_git_checkout(env_dir: Path) -> bool:
    """Whether *env_dir* holds a git checkout — never reap one if so (#291).

    A managed auto-isolated env dir holds only a sqlite DB + ``logs/`` and is
    never a git checkout. A ``.git`` entry — a dir (real repo) or a file (linked
    worktree) — means an unexpected checkout landed here, where uncommitted or
    unpushed work could live. Such a dir is kept defensively, mirroring the
    #706/#835 data-loss discipline: only no-checkout dirs are ever reaped. The
    ``.git`` presence is the precise signal — working-tree state only exists when
    a ``.git`` is present, so this one check covers both "real checkout" and "any
    uncommitted/unpushed work".
    """
    return (env_dir / ".git").exists()


def _changed_since(env_dir: Path, snapshot_at: float) -> bool:
    """Whether *env_dir* was created or written at/after the keep-set was computed.

    The box provisions continuously, so an env dir can be minted between the
    evidence gathering and this dir's turn in the loop — absent from the keep-set
    through no fault of its own, and live. An unreadable stat reads as changed:
    a dir that cannot be examined is never provably dead.
    """
    try:
        return env_dir.stat().st_mtime >= snapshot_at
    except OSError:
        return True


def _protected_reason(env_dir: Path, *, keep_unmappable_live: bool) -> str | None:
    """Why *env_dir* is off-limits whatever the evidence says about its owner."""
    if is_clean_ignored(env_dir.name):
        return "matches clean_ignore"
    if _holds_git_checkout(env_dir):
        return "holds a git checkout (uncommitted/unpushed work)"
    if keep_unmappable_live:
        return "a live worktree has no recorded checkout path — cannot prove this env dir is orphan (live work)"
    return None


def _evidence_gap(env_dir: Path, *, stamp: owner_stamps.OwnerStamp, live: LiveCheckoutSlugs) -> str | None:
    """Why this pass's evidence cannot speak about *env_dir*, or ``None`` when it can.

    Three distinct shortfalls, widest first: a read that failed and hid an unknown
    number of live checkouts, a dir minted after the evidence was gathered, and the
    #3872 one — a stamped owner this venue could never have seen, or no stamp at all.
    Absence of an owner is not proof of death while any of these holds (#706).
    """
    if live.gaps:
        return f"checkout evidence is incomplete ({'; '.join(live.gaps)}) — cannot prove any dir is orphan"
    if _changed_since(env_dir, live.snapshot_at):
        return "changed after the keep-set was computed — outside this pass's evidence"
    return stamp.missing_evidence


def _keep_reason(env_dir: Path, *, live: LiveCheckoutSlugs, keep_unmappable_live: bool) -> str | None:
    """Why *env_dir* must survive this pass, or ``None`` when it is provably reclaimable.

    Ownership proof first, then the pins that hold regardless, then the ways the
    evidence falls short. The single ``None`` exit is the only path to a deletion.
    """
    if env_dir.name in live.slugs:
        return "a live checkout owns it"
    stamp = owner_stamps.read_owner_stamp(env_dir, live.scanned_roots)
    return (
        stamp.proof_of_life
        or _protected_reason(env_dir, keep_unmappable_live=keep_unmappable_live)
        or _evidence_gap(env_dir, stamp=stamp, live=live)
    )


def reap_orphan_isolated_worktree_roots(workspace: Path, *, dry_run: bool = False) -> list[str]:
    """Remove the auto-isolated worktree env dirs PROVEN dead (#291, #3852, #3872).

    Each git worktree gets an auto-isolated env dir under
    :func:`paths.auto_isolated_worktrees_dir` (``db.sqlite3`` + ``logs/``). A dir is
    reclaimable only when its own stamp names a checkout that does not exist AND lies
    within a root this venue walked — the one shape in which "no owner found" and "no
    owner exists" are the same answer. Every other verdict keeps it: the union keep-set
    (registered rows plus every checkout git reports) proving it live, a stamp naming a
    live checkout, or evidence that falls short of proof. Only immediate child
    *directories* of the root are considered; loose files (seed locks) are ignored.

    Every dir gets a line naming its disposition and reason, in a live run and a
    preview alike, so ``--dry-run`` is a full account of what the live run would
    do rather than a list of its deletions.

    Four fail-closed guards, all #706-shaped. Unreadable git evidence (``live.gaps``)
    keeps EVERY dir: an unread clone hides an unknown number of live checkouts. A BUSY
    worktree row with no recorded checkout path (:func:`_has_unmappable_live_worktree`)
    cannot be hashed to a slug, so its in-use isolated DB is indistinguishable from an
    orphan. And the two #3872 guards: a stamped owner this venue could never have
    observed, and a dir carrying no stamp at all, are both missing evidence rather than
    dead — which is why a venue blind to the clones now reclaims nothing instead of
    everything.
    """
    root = paths.auto_isolated_worktrees_dir()
    if not root.is_dir():
        return []
    live = _live_checkout_slugs(workspace, root)
    keep_unmappable_live = _has_unmappable_live_worktree()
    outcomes: list[str] = []
    for env_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        reason = _keep_reason(env_dir, live=live, keep_unmappable_live=keep_unmappable_live)
        if reason is not None:
            outcomes.append(f"KEPT '{env_dir.name}': {reason}")
        elif dry_run:
            outcomes.append(preview_line(f"Remove orphan isolated env dir: {env_dir.name}", dry_run=True))
        else:
            shutil.rmtree(env_dir)
            outcomes.append(f"Removed orphan isolated worktree root: {env_dir.name}")
    return outcomes
