"""The branch-upstream invariant: a branch tracks its OWN remote ref, or nothing.

``git worktree add -b <branch> <path> origin/main`` — the recipe this repo
published for cutting an ad-hoc worktree — makes git configure the new branch to
track ``origin/main``, so ``branch.<branch>.merge`` reads ``refs/heads/main``.
``push.default = upstream`` would then aim a routine ``git push`` on such a
branch straight at ``main``, and under git's unchosen ``simple`` default the
same config makes ``git push`` refuse over the mismatched names instead.

Conformance is therefore two-valued, not one: the merge ref is the branch's own,
or there is no upstream at all. Pointing a never-pushed branch at its own absent
remote ref is NOT the third option it looks like — git then renders the branch
``[gone]``, the exact signal
:func:`~teatree.core.management.commands._workspace.cleanup.prune_branches`
force-deletes on.
"""

from dataclasses import dataclass, replace

from teatree.utils.git_run import check, run

_DEFAULT_REMOTE = "origin"


@dataclass(frozen=True)
class BranchUpstream:
    """One local branch's configured upstream, classified against its own ref."""

    branch: str
    remote: str
    merge_ref: str
    own_remote_ref_exists: bool

    @property
    def own_merge_ref(self) -> str:
        return f"refs/heads/{self.branch}"

    @property
    def is_conformant(self) -> bool:
        return self.merge_ref in {"", self.own_merge_ref}

    @property
    def effective_remote(self) -> str:
        """The remote the branch's own ref would live on.

        A blank ``branch.<n>.remote``, or the ``.`` git writes for local-only
        tracking, names no remote to look the branch up on. ``origin`` is the
        only candidate left, and guessing wrong merely reports "no own remote
        ref", which is the conservative answer.
        """
        return self.remote if self.remote and self.remote != "." else _DEFAULT_REMOTE

    @property
    def remedy(self) -> str:
        """The exact git command that repairs this branch, run from its clone."""
        if self.own_remote_ref_exists:
            return f"git branch --set-upstream-to={self.effective_remote}/{self.branch} {self.branch}"
        return f"git branch --unset-upstream {self.branch}"


def _configured_upstreams(repo: str) -> dict[str, BranchUpstream]:
    """Every branch's configured upstream from ONE config read, remote lookup not yet done.

    Read off the config keys rather than ``for-each-ref --format=%(upstream)``:
    the question is what is CONFIGURED, and ``%(upstream)`` renders a resolved
    ref that hides whether the merge ref names this branch or a different one.
    Splitting on the key's fixed ``branch.``/``.merge``/``.remote`` affixes keeps
    a branch name containing dots intact.
    """
    fields: dict[str, dict[str, str]] = {}
    for line in run(repo=repo, args=["config", "--get-regexp", r"^branch\..*\.(merge|remote)$"]).splitlines():
        key, _, value = line.partition(" ")
        branch, _, field = key.removeprefix("branch.").rpartition(".")
        if branch and field in {"merge", "remote"}:
            fields.setdefault(branch, {})[field] = value.strip()
    return {
        branch: BranchUpstream(
            branch=branch,
            remote=found.get("remote", ""),
            merge_ref=found.get("merge", ""),
            own_remote_ref_exists=False,
        )
        for branch, found in fields.items()
    }


def _with_remote_lookup(repo: str, entry: BranchUpstream) -> BranchUpstream:
    """*entry* with its own-remote-ref existence resolved — the one git call per branch."""
    ref = f"refs/remotes/{entry.effective_remote}/{entry.branch}"
    exists = bool(run(repo=repo, args=["rev-parse", "--verify", "--quiet", ref]))
    return replace(entry, own_remote_ref_exists=exists)


def branch_upstream(repo: str, branch: str) -> BranchUpstream:
    """*branch*'s configured upstream in *repo*, with its own-remote-ref lookup resolved."""
    unresolved = _configured_upstreams(repo).get(
        branch, BranchUpstream(branch=branch, remote="", merge_ref="", own_remote_ref_exists=False)
    )
    return _with_remote_lookup(repo, unresolved)


def mistracked_branches(repo: str) -> list[BranchUpstream]:
    """Every branch in *repo* whose upstream merge ref is not its own, name-sorted.

    The remote-ref lookup runs only for the branches that turn out mistracked —
    conformance is decided from the config read alone, so a healthy clone of a
    few hundred branches costs one git call rather than one per branch.
    """
    configured = sorted(_configured_upstreams(repo).values(), key=lambda entry: entry.branch)
    return [_with_remote_lookup(repo, entry) for entry in configured if not entry.is_conformant]


def normalize_branch_upstream(repo: str, branch: str) -> str:
    """Repair one branch's upstream; ``""`` when it was already conformant.

    Verify-by-re-read: the outcome is re-classified from git rather than taken
    from the write's exit code, so a repair git accepted but did not apply is
    reported as a failure carrying the manual remedy instead of as a success.
    """
    entry = branch_upstream(repo, branch)
    if entry.is_conformant:
        return ""
    if entry.own_remote_ref_exists:
        check(repo=repo, args=["branch", f"--set-upstream-to={entry.effective_remote}/{branch}", branch])
    else:
        check(repo=repo, args=["branch", "--unset-upstream", branch])
    after = branch_upstream(repo, branch)
    if not after.is_conformant:
        return f"FAILED {branch}: upstream is still {after.merge_ref} — run: {after.remedy}"
    return f"Repaired {branch}: {entry.merge_ref} -> {after.merge_ref or 'unset (no remote branch to track)'}"


def repair_mistracked_branches(repo: str, *, dry_run: bool = False) -> list[str]:
    """Repair every mistracked branch in *repo*. Idempotent — a clean repo returns ``[]``."""
    entries = mistracked_branches(repo)
    if dry_run:
        return [f"Would repair {entry.branch} ({entry.merge_ref}): {entry.remedy}" for entry in entries]
    return [normalize_branch_upstream(repo, entry.branch) for entry in entries]


__all__ = [
    "BranchUpstream",
    "branch_upstream",
    "mistracked_branches",
    "normalize_branch_upstream",
    "repair_mistracked_branches",
]
