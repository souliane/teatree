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

**A scan result is venue-dependent; the owner stamp is not (#3872).** The
isolated-env root is routinely shared into a container while the clones that own
those dirs are not, so the blindness runs BOTH ways: run in two venues, each
proposes removing dirs the other keeps as live. Scanning harder cannot fix that —
a scan cannot see what is not mounted. So a stamped owner that does not exist is
only DEAD when this venue can see the filesystem it lived on
(:meth:`teatree.paths.IsolatedEnvDir.owner_liveness`); otherwise it is UNKNOWN,
which keeps the dir and reports why. An UNSTAMPED dir is still judged on the
other evidence — refusing those too is only safe once every venue has backfilled,
so it is a later stage, not this one.

Incomplete evidence fails CLOSED — an unreadable directory, a subtree past the
walk's depth cap, or an unreadable clone registry hides an unknown number of live
checkouts, so every otherwise-unreferenced dir is kept and the gap reported. That
rests on the scan reporting everything it did not cover: a skip recording no gap
left ``complete`` true while the keep-set was short, which is what authorised
proposing a live checkout's env dir for deletion (#3872). A dir modified at or
after the keep-set instant is kept for the same reason: the evidence never
covered it.
"""

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from teatree import paths
from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.gates.idle_stack import worktree_protects_against_reap
from teatree.core.management.commands._workspace import checkout_registry
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


def _stamp_discovered_owners(checkouts: frozenset[str], root: Path) -> None:
    """Record each discovered checkout as the owner of its env dir.

    Backfill, so the invertible mapping covers what already exists rather than
    only dirs minted after the stamp shipped — it protected 3 of 185 dirs on the
    host until this ran. Writing it here also means a dir's ownership survives the
    checkout later becoming undiscoverable.
    """
    for checkout in checkouts:
        env_dir = root / paths.isolated_slug(Path(checkout))
        if env_dir.is_dir():
            paths.IsolatedEnvDir(env_dir).stamp_owner(Path(checkout))


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
    _stamp_discovered_owners(registry.paths, root)
    slugs = _row_referenced_slugs()
    slugs.update(paths.isolated_slug(Path(checkout)) for checkout in registry.paths)
    return LiveCheckoutSlugs(frozenset(slugs), registry.gaps, snapshot_at)


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


def _owned_by_live_checkout(env_dir: Path, live: LiveCheckoutSlugs) -> str | None:
    """The evidence that a live checkout owns *env_dir*, or ``None`` when none does.

    The two positive proofs, strongest reported first: a slug the union keep-set
    knows, or the dir's own stamp naming a checkout that still exists.
    """
    if env_dir.name in live.slugs:
        return "a live checkout owns it"
    env = paths.IsolatedEnvDir(env_dir)
    if env.owner_liveness() is paths.OwnerLiveness.ALIVE:
        return f"its owner stamp names a live checkout ({env.owner})"
    return None


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


def _outside_this_passs_evidence(env_dir: Path, live: LiveCheckoutSlugs) -> str | None:
    """Why this pass's evidence never covered *env_dir* — an unknown, not a death.

    All three are scope failures rather than findings: the walk left gaps, the
    owner lives on a filesystem this venue cannot reach, or the dir was minted
    after the keep-set was taken.
    """
    if live.gaps:
        return f"checkout evidence is incomplete ({'; '.join(live.gaps)}) — cannot prove any dir is orphan"
    env = paths.IsolatedEnvDir(env_dir)
    if env.owner_liveness() is paths.OwnerLiveness.UNKNOWN:
        return (
            f"its owner stamp names a path this venue cannot see ({env.owner}) — missing evidence, not proof of death"
        )
    if _changed_since(env_dir, live.snapshot_at):
        return "changed after the keep-set was computed — outside this pass's evidence"
    return None


def _unprovable_reason(env_dir: Path, *, live: LiveCheckoutSlugs, keep_unmappable_live: bool) -> str | None:
    """Why *env_dir*'s death cannot be PROVEN, even though nothing claims to own it.

    Each branch is a distinct way the evidence falls short of proof — evidence
    that never covered this dir, an ignore glob, unexpected git work, an
    unmappable live row. Absence of an owner is not proof of death while any of
    these holds (the #706 standard).
    """
    uncovered = _outside_this_passs_evidence(env_dir, live)
    if uncovered is not None:
        return uncovered
    if is_clean_ignored(env_dir.name):
        return "matches clean_ignore"
    if _holds_git_checkout(env_dir):
        return "holds a git checkout (uncommitted/unpushed work)"
    if keep_unmappable_live:
        return "a live worktree has no recorded checkout path — cannot prove this env dir is orphan (live work)"
    return None


def _keep_reason(env_dir: Path, *, live: LiveCheckoutSlugs, keep_unmappable_live: bool) -> str | None:
    """Why *env_dir* must survive this pass, or ``None`` when it is provably reclaimable.

    Ownership proof first, then the reasons its death cannot be proven. The single
    ``None`` exit is the only path to a deletion.
    """
    owned = _owned_by_live_checkout(env_dir, live)
    if owned is not None:
        return owned
    return _unprovable_reason(env_dir, live=live, keep_unmappable_live=keep_unmappable_live)


def reap_orphan_isolated_worktree_roots(workspace: Path, *, dry_run: bool = False) -> list[str]:
    """Remove the auto-isolated worktree env dirs no live checkout owns (#291, #3852).

    Each git worktree gets an auto-isolated env dir under
    :func:`paths.auto_isolated_worktrees_dir` (``db.sqlite3`` + ``logs/``). When
    the checkout is gone but its env dir lingers, the dir is an orphan.
    Ownership is resolved by :func:`_live_checkout_slugs` — registered rows UNION
    every checkout git reports — plus each dir's own owner stamp, so the reaper
    and :func:`paths.resolve_data_dir` agree on the population the slug mapping
    spans. Only immediate child *directories* of the root are considered; loose
    files (seed locks) are ignored.

    Every dir gets a line naming its disposition and reason, in a live run and a
    preview alike, so ``--dry-run`` is a full account of what the live run would
    do rather than a list of its deletions.

    Two fail-closed guards, both #706-shaped. Unreadable git evidence
    (``live.gaps``) keeps EVERY dir: an unread clone hides an unknown number of
    live checkouts. And a BUSY worktree row with no recorded checkout path
    (:func:`_has_unmappable_live_worktree`) cannot be hashed to a slug, so its
    in-use isolated DB is indistinguishable from an orphan — keep them all rather
    than reap a live control DB out from under a mid-task agent.
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
