"""Orphaned-stash reaping for the ``t3 <overlay> workspace clean-all`` subcommand.

Split out of :mod:`teatree.core.management.commands._workspace.cleanup` so that
module stays under the module-health LOC cap. The forge-CLI-free squash-merge
signal it relies on (:func:`_branch_captured_upstream`) lives in
:mod:`teatree.core.worktree.branch_classification` — the branch/worktree reapers share it.
"""

import re
from dataclasses import dataclass

from teatree.core.management.commands._workspace.preview import preview_line
from teatree.core.worktree.branch_classification import _branch_captured_upstream
from teatree.utils import git
from teatree.utils.run import CommandFailedError

_STASH_BRANCH_RE = re.compile(r"^stash@\{\d+\}:\s+(?:WIP on|On)\s+(?P<branch>[^:]+):")

#: ``<sha>\t<selector>\t<subject>`` — identity, current selector and message from
#: ONE read, so the three can never describe different entries.
_STASH_LIST_FORMAT = "--format=%H%x09%gd%x09%gs"


@dataclass(frozen=True)
class _StashEntry:
    """One ``git stash list`` row, carrying the commit that IS the entry."""

    sha: str
    selector: str
    subject: str

    @property
    def line(self) -> str:
        return f"{self.selector}: {self.subject}"


def _list_stashes(repo: str) -> list[_StashEntry]:
    raw = git.run(repo=repo, args=["stash", "list", _STASH_LIST_FORMAT])
    entries: list[_StashEntry] = []
    for row in raw.splitlines():
        sha, _, rest = row.partition("\t")
        selector, _, subject = rest.partition("\t")
        if sha and selector:
            entries.append(_StashEntry(sha=sha.strip(), selector=selector.strip(), subject=subject))
    return entries


def _current_selector(repo: str, sha: str) -> str | None:
    """The selector *sha* occupies RIGHT NOW, or ``None`` when gone or ambiguous.

    ``refs/stash`` lives in the COMMON dir, so a sibling worktree's ``git stash
    push`` renumbers every entry for every other checkout with no action by it.
    A selector resolved when an entry was JUDGED therefore names a different
    commit by the time the drop runs — which is how a reaper that found entry A
    merged-and-safe drops entry B holding unmerged work. Re-resolving by identity
    immediately before the drop is what keeps the two the same entry.

    Two rows sharing a sha (an identical tree, parent and message) are refused
    rather than guessed at: ``git stash drop`` takes one selector, and picking
    either would be a coin flip over someone's only copy.
    """
    matches = [entry.selector for entry in _list_stashes(repo) if entry.sha == sha]
    return matches[0] if len(matches) == 1 else None


def _stash_branch(line: str) -> str:
    """Return the branch a ``git stash list`` line belongs to, or ``""`` if unparsable.

    A stash taken on a detached HEAD reads ``On (no branch): ...`` — there is no
    owning branch to compare against, so it is reported as unparsable and the
    stash is kept rather than reaped.
    """
    match = _STASH_BRANCH_RE.match(line)
    if not match:
        return ""
    branch = match.group("branch").strip()
    return "" if branch == "(no branch)" else branch


def drop_orphaned_stashes(repo: str, *, dry_run: bool = False) -> list[str]:
    """Drop stashes whose branch is gone — but ONLY when their changes are merged.

    A stash is the *only* copy of its work. Dropping it because its owning branch
    no longer exists is silent data loss when the stashed changes were never
    merged — the exact failure that strands work like the #1913 FSM and dreaming
    phase stashes. So an orphaned stash is reaped only when its diff is already
    captured upstream (the same patch-id squash-merge signal
    :func:`_branch_captured_upstream` gives the worktree/branch reapers); an
    orphaned stash carrying UNMERGED work is KEPT with a warning, never dropped.
    A probe failure reads as not-merged, so uncertainty keeps the stash.

    Every judgement and every drop is keyed on the entry's own commit sha, never
    on the ``stash@{i}`` position it happened to hold: ``refs/stash`` is shared
    across a clone's worktrees, so a sibling's push renumbers the stack with no
    action by this caller.
    """
    entries = _list_stashes(repo)
    if not entries:
        return []

    existing = {
        line.strip().removeprefix("* ").removeprefix("+ ")
        for line in git.run(repo=repo, args=["branch", "--no-color"]).splitlines()
    }
    try:
        default = (
            git.run_strict(repo=repo, args=["rev-parse", "--abbrev-ref", "origin/HEAD"]).strip().removeprefix("origin/")
        )
    except CommandFailedError:
        # STRICT so a missing ``origin/HEAD`` actually raises (the lenient runner
        # made this ``except`` dead). The "main" fallback below stays.
        default = ""
    default = default or "main"

    cleaned: list[str] = []
    for entry in entries:
        branch = _stash_branch(entry.line)
        if not branch or branch in existing:
            continue
        label = entry.selector
        if not _branch_captured_upstream(repo, entry.sha, default):
            cleaned.append(
                f"Kept orphaned stash {label} (was on {branch}): changes are NOT merged — "
                f"dropping would lose them. Recover with `git stash apply {entry.sha}`."
            )
            continue
        if dry_run:
            cleaned.append(
                preview_line(f"Drop orphaned stash: {label} (was on {branch}; changes already merged)", dry_run=True)
            )
            continue
        selector = _current_selector(repo, entry.sha)
        if selector is None:
            cleaned.append(
                f"Kept orphaned stash {label} (was on {branch}): its identity {entry.sha[:12]} "
                f"no longer resolves to exactly one entry — the stash stack moved under us. Re-run clean-all."
            )
            continue
        try:
            # STRICT: the lenient runner swallows a refused drop to "", so a stash
            # still on the stack was reported as reaped and never looked at again.
            git.run_strict(repo=repo, args=["stash", "drop", selector])
        except CommandFailedError as exc:
            cleaned.append(f"Kept orphaned stash {label} (was on {branch}): `git stash drop` failed — {exc}")
            continue
        cleaned.append(f"Dropped orphaned stash: {label} (was on {branch}; changes already merged)")

    return cleaned
