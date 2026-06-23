"""Pull work-repo main clones after a merge — keep them current per tick.

When a ticket/MR/PR merges, the corresponding work-repo *main clone*
under ``$T3_WORKSPACE_DIR`` (the clone a feature worktree is created
from) drifts behind ``origin/<default-branch>`` until a human remembers
to ``git pull`` it. A stale main clone silently poisons investigations:
``git show`` / ``grep`` against a clone parked one merge behind — or, in
the worst case, left on a feature branch from an earlier checkout —
returns wrong answers. This scanner closes the loop the same way the
self-update scanner keeps the *editable* clones current, but for the
*work-repo* clones the agent reasons against.

"A merge happened since last tick" is detected the robust, host-agnostic
way: a merge is precisely the event that advances ``origin/<default>``.
Every tick (subject to the cadence gate) the scanner ``git fetch``es
each clone's origin and — when the local default-branch HEAD now trails
the remote — fast-forwards it. An already-current clone is a no-op
(``up_to_date``) that emits no ``updated`` signal, so re-scanning a
current clone never spams.

The scanner is a strict superset of nothing destructive: it only ever
``pull --ff-only``. A non-default-branch checkout or a non-fast-forward
remote is a *skip with a reason*, never a reset / force / stash. A
tracked-dirty working tree is normally a skip too, with ONE content-safe
exception (#2614): when EVERY dirty path's blob is provably byte-identical
to ``origin/<default>`` (the change already landed upstream via a proper
PR — an empty ``git diff origin/<default> -- <path>`` for both the
worktree and the index), the stale duplicate is auto-discarded and the FF
pull proceeds. This is data-loss-free by that precondition; any path whose
blob DIFFERS from origin keeps the whole skip-with-warning — genuine local
work is never reset, forced, or stashed. This mirrors the prove-then-act
discipline of :mod:`teatree.cli._update_reconcile` (#2607). It is the same
safe primitive as :class:`teatree.loop.scanners.self_update.SelfUpdateScanner`;
the two scanners walk disjoint clone sets (editable installs vs. work-repo
clones) on independent cadence ledgers.

Decision ladder per clone (per tick):

1. cadence elapsed since last pull? → skip (``cadence_not_elapsed``)
2. repo path missing on disk? → ``failed`` (logged + signal emitted)
3. ``git fetch origin`` fails? → ``failed``
4. on a non-default branch? → ``skipped`` (``branch=<name>``)
5. tracked-dirty working tree, every dirty blob == ``origin/<default>``?
    → auto-discard the stale duplicate(s), then fall through (#2614 self-heal)
6. tracked-dirty working tree, any dirty blob differs? → ``skipped`` (``dirty_tracked``)
7. up to date already? → ``up_to_date``
8. ``git pull --ff-only`` advances HEAD? → ``updated``
9. ``git pull --ff-only`` refuses (non-ff divergence)? → ``failed``

The post-pass :class:`PullMainCloneMarker` row records the outcome + the
new HEAD SHA so the cadence gate can short-circuit cheaply on the next
tick without re-shelling git.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal
from teatree.utils.run import CompletedProcess, run_allowed_to_fail

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PullOutcome:
    """Internal record of one clone's pass through the decision ladder."""

    outcome: str  # "updated" | "up_to_date" | "skipped" | "failed" | "cadence_not_elapsed"
    reason: str = ""
    old_sha: str = ""
    new_sha: str = ""


@dataclass(slots=True)
class PullMainCloneScanner:
    """Fast-forward each configured work-repo main clone to ``origin/<default>``.

    *repos* is an ordered list of ``(label, path)`` pairs; *label* is the
    stable identity used both for the persisted :class:`PullMainCloneMarker`
    row and for the emitted signal payloads. The wiring layer namespaces
    the label with the overlay name (``"<overlay>:<repo>"``) so two
    overlays sharing a repo basename keep independent cadence ledgers.
    *cadence_hours* gates how often the scanner is allowed to issue git
    operations against a given clone — decoupled from the loop tick
    cadence so a sub-minute tick doesn't degenerate into sub-minute git
    fetches against every work repo.

    The scanner is a stateless pure-Python object; all persistence is in
    :class:`PullMainCloneMarker`, all logging goes through ``logger`` so
    the tick-orchestrator's pipeline picks it up without special-casing.
    """

    repos: tuple[tuple[str, Path], ...] = ()
    cadence_hours: int = 1
    name: str = "pull_main_clone"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for label, path in self.repos:
            outcome = self._process_one(label=label, path=path)
            signals.append(_signal_from_outcome(label=label, outcome=outcome))
            logger.info(
                "pull_main_clone %s outcome=%s reason=%s",
                label,
                outcome.outcome,
                outcome.reason,
            )
        return signals

    def _process_one(self, *, label: str, path: Path) -> _PullOutcome:
        if self._cadence_blocks(label=label):
            return _PullOutcome(outcome="cadence_not_elapsed", reason="recent_marker")
        if not path.is_dir():
            return _record_marker(
                label=label,
                path=path,
                outcome=_PullOutcome(outcome="failed", reason=f"repo_path_missing:{path}"),
            )
        outcome = _attempt_pull(repo=path)
        return _record_marker(label=label, path=path, outcome=outcome)

    def _cadence_blocks(self, *, label: str) -> bool:
        """Return True iff a recent enough marker for *label* exists."""
        # Import inside the method so the scanner module imports cleanly even
        # when Django app loading hasn't run yet (the wiring layer imports
        # this class at module load time).
        from teatree.core.models.pull_main_clone_marker import PullMainCloneMarker  # noqa: PLC0415

        marker = PullMainCloneMarker.objects.filter(repo_label=label).first()
        if marker is None:
            return False
        elapsed_hours = (timezone.now() - marker.last_pull_at).total_seconds() / 3600.0
        return elapsed_hours < self.cadence_hours


def _record_marker(*, label: str, path: Path, outcome: _PullOutcome) -> _PullOutcome:
    """Upsert the :class:`PullMainCloneMarker` row + return *outcome*."""
    from teatree.core.models.pull_main_clone_marker import PullMainCloneMarker  # noqa: PLC0415

    sha = outcome.new_sha or outcome.old_sha
    try:
        with transaction.atomic():
            PullMainCloneMarker.objects.update_or_create(
                repo_label=label,
                defaults={
                    "repo_path": str(path),
                    "last_outcome": outcome.outcome,
                    "last_reason": outcome.reason[:200],
                    "last_pulled_sha": sha,
                    "last_pull_at": timezone.now(),
                },
            )
    except Exception:
        # Persisting the marker must never crash the tick — log and let the
        # scanner emit its signal anyway. The next tick will try again; the
        # worst case is one extra git fetch.
        logger.exception("pull_main_clone failed to upsert PullMainCloneMarker for %s", label)
    return outcome


def _signal_from_outcome(*, label: str, outcome: _PullOutcome) -> ScanSignal:
    kind = f"pull_main_clone.{outcome.outcome}"
    summary = f"pull-main-clone {label}: {outcome.outcome}"
    if outcome.reason:
        summary = f"{summary} ({outcome.reason})"
    return ScanSignal(
        kind=kind,
        summary=summary,
        payload={
            "repo": label,
            "outcome": outcome.outcome,
            "reason": outcome.reason,
            "old_sha": outcome.old_sha,
            "new_sha": outcome.new_sha,
        },
    )


def _attempt_pull(*, repo: Path) -> _PullOutcome:
    """Run the per-clone decision ladder against an existing clone.

    Only the safe ``fetch`` + ``pull --ff-only`` primitive plus the safety
    gates (origin remote present, default branch resolved, branch matches
    default, tracked-clean tree). A non-fast-forward divergence makes the
    ``pull --ff-only`` exit non-zero — that surfaces as ``failed``, never
    a reset/force. ``old_sha`` on every outcome carries the pre-pass HEAD
    so the persisted marker records what the clone was anchored on, even
    on a skip.
    """
    pre_sha = _full_sha(repo)
    pre_check = _pre_pull_gate(repo=repo, pre_sha=pre_sha)
    if pre_check is not None:
        return pre_check
    pull = _git(repo, "pull", "--ff-only")
    if pull.returncode != 0:
        return _PullOutcome(outcome="failed", reason=f"pull:{pull.stderr.strip()[:120]}", old_sha=pre_sha)
    new_sha = _full_sha(repo)
    if new_sha == pre_sha:
        return _PullOutcome(outcome="up_to_date", old_sha=pre_sha, new_sha=new_sha)
    return _PullOutcome(outcome="updated", old_sha=pre_sha, new_sha=new_sha)


def _pre_pull_gate(*, repo: Path, pre_sha: str) -> _PullOutcome | None:
    """Run fetch + branch/clean safety gates; return ``None`` when clear to pull."""
    fetch = _git(repo, "fetch", "origin")
    if fetch.returncode != 0:
        return _PullOutcome(outcome="failed", reason=f"fetch:{fetch.stderr.strip()[:120]}", old_sha=pre_sha)
    default_branch = _default_branch(repo)
    if default_branch is None:
        return _PullOutcome(outcome="skipped", reason="no_origin_head", old_sha=pre_sha)
    current = _current_branch(repo)
    if current != default_branch:
        return _PullOutcome(
            outcome="skipped",
            reason=f"branch={current}!={default_branch}",
            old_sha=pre_sha,
        )
    dirty = _tracked_dirty_paths(repo)
    if dirty:
        if _all_dirty_blobs_match_origin(repo=repo, default_branch=default_branch, dirty=dirty):
            _discard_paths(repo=repo, paths=dirty)
            return None
        return _PullOutcome(
            outcome="skipped",
            reason=f"dirty_tracked:{','.join(dirty)[:80]}",
            old_sha=pre_sha,
        )
    return None


def _git(repo: Path, *args: str) -> CompletedProcess[str]:
    """Run ``git`` in *repo*; never raise on non-zero exit (caller branches on returncode)."""
    return run_allowed_to_fail(["git", *args], cwd=repo, expected_codes=None)


def _full_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _default_branch(repo: Path) -> str | None:
    result = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().rsplit("/", 1)[-1]


def _tracked_dirty_paths(repo: Path) -> list[str]:
    """Return paths with uncommitted *tracked* changes — untracked are not blockers."""
    result = _git(repo, "status", "--porcelain")
    return [line[3:] for line in result.stdout.splitlines() if line and not line.startswith("??")]


def _all_dirty_blobs_match_origin(*, repo: Path, default_branch: str, dirty: list[str]) -> bool:
    """True iff discarding EVERY dirty path is provably data-loss-free vs ``origin/<default>`` (#2614).

    The content-safe self-heal precondition. Discarding a path resets both its
    working-tree blob and its index blob to HEAD, after which the FF pull advances
    HEAD to ``origin/<default>``. So discarding is data-loss-free for a path iff
    every blob it would drop is already either upstream or HEAD-about-to-become-
    upstream. Concretely, a path qualifies only when BOTH conditions hold.

    Working-tree condition: its working-tree blob is byte-identical to
    ``origin/<default>`` — the visible content the operator sees is already
    upstream (an EMPTY ``git diff origin/<default> -- <path>``). This is the
    issue's named check.

    Index condition: its index blob is byte-identical to ``origin/<default>`` (a
    staged duplicate — the exact incident shape) OR to HEAD (nothing staged
    beyond HEAD, i.e. an unstaged-only modification) — so the staged blob, if
    any, also carries no content that is not already upstream.

    Either condition being indeterminate means the path is NOT provably safe, so
    the WHOLE set fails and the caller keeps the safe skip — never a partial reset.
    A ``git diff --quiet`` exit >1 (a git error) is treated as not-clean
    (fail-closed): an inconclusive content check never authorizes a discard.
    """
    target = f"origin/{default_branch}"
    for path in dirty:
        worktree_vs_origin = _diff_is_empty(repo, target, path)
        if not worktree_vs_origin:
            return False
        index_vs_origin = _diff_is_empty(repo, target, path, cached=True)
        index_vs_head = _diff_is_empty(repo, "HEAD", path, cached=True)
        if not (index_vs_origin or index_vs_head):
            return False
    return True


def _diff_is_empty(repo: Path, target: str, path: str, *, cached: bool = False) -> bool:
    """True iff ``git diff --quiet [<--cached>] <target> -- <path>`` reports no diff.

    ``--quiet`` exits 0 == no diff, 1 == diff present, >1 == error. Only a clean
    exit 0 is treated as an empty diff; a git error (>1) is treated as a present
    diff so an inconclusive check never authorizes a discard (fail-closed).
    """
    args = ["diff", "--quiet"]
    if cached:
        args.append("--cached")
    args += [target, "--", path]
    return _git(repo, *args).returncode == 0


def _discard_paths(*, repo: Path, paths: list[str]) -> None:
    """Discard staged + working-tree changes for *paths*, restoring them to HEAD.

    Reached ONLY after :func:`_all_dirty_blobs_match_origin` proved every path's
    blob is byte-identical to ``origin/<default>`` — so the discarded content is
    already upstream and the ensuing FF pull re-materialises it. Data-loss-free
    by that precondition; this helper never runs against a differing blob.
    """
    _git(repo, "restore", "--staged", "--worktree", "--", *paths)


__all__ = ["PullMainCloneScanner"]
