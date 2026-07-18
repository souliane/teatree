"""Git operations facade — re-exports the cohesive sibling modules.

The git surface is partitioned by concern across sibling modules under
``teatree.utils``:

- :mod:`teatree.utils.git_run` — low-level ``git -C`` runners + ``GIT_*`` env helper.
- :mod:`teatree.utils.git_branch` — branch/ref discovery.
- :mod:`teatree.utils.git_commit` — commit/log/rev-list/message ops.
- :mod:`teatree.utils.git_status` — working-tree status + diff capture.
- :mod:`teatree.utils.git_sync` — fetch/rebase/merge/pull/push.
- :mod:`teatree.utils.git_worktree` — worktree management + teardown guards.
- :mod:`teatree.utils.git_remote_ops` — invoking remote/config ops.

This module keeps the :class:`GitRepo` OOP wrapper and re-exports the full
public surface so every ``from teatree.utils.git import ...`` and ``git.<fn>``
attribute access keeps resolving unchanged, with identity preserved
(``git.merge_base is git_commit.merge_base``). Re-exporting (not redefining)
the names is what guarantees that identity: a test that patches ``git.<fn>``
still drives a production caller reading ``git.<fn>``.
"""

from teatree.utils.git_branch import (
    DETACHED_HEAD,
    branch_delete,
    branch_merged,
    current_branch,
    default_branch,
    head_sha,
)
from teatree.utils.git_commit import (
    branch_diff,
    commit,
    commit_messages,
    first_commit_message,
    last_commit_message,
    log_oneline,
    merge_base,
    rev_count,
    soft_reset,
    unsynced_commits,
)
from teatree.utils.git_remote_ops import config_value, remote_slug, remote_url
from teatree.utils.git_run import check, git_env_hermetic, git_env_without_overrides, run, run_strict
from teatree.utils.git_status import full_worktree_diff, status_porcelain, status_porcelain_strict
from teatree.utils.git_sync import fetch, merge_abort, merge_no_edit, pull_ff_only, push, rebase
from teatree.utils.git_worktree import (
    commits_absent_from_all_remotes,
    locked_worktree_paths,
    recovered_head_sha_after_ref_gone,
    worktree_add,
    worktree_add_at_ref,
    worktree_move,
    worktree_remove,
)
from teatree.utils.git_worktree_query import (
    WorktreeRecord,
    canonical_repo_root,
    git_common_dir,
    is_git_checkout,
    list_worktrees,
    worktree_for_branch,
)

__all__ = [
    "DETACHED_HEAD",
    "GitRepo",
    "WorktreeRecord",
    "branch_delete",
    "branch_diff",
    "branch_merged",
    "canonical_repo_root",
    "check",
    "commit",
    "commit_messages",
    "commits_absent_from_all_remotes",
    "config_value",
    "current_branch",
    "default_branch",
    "fetch",
    "first_commit_message",
    "full_worktree_diff",
    "git_common_dir",
    "git_env_hermetic",
    "git_env_without_overrides",
    "head_sha",
    "is_git_checkout",
    "last_commit_message",
    "list_worktrees",
    "locked_worktree_paths",
    "log_oneline",
    "merge_abort",
    "merge_base",
    "merge_no_edit",
    "pull_ff_only",
    "push",
    "rebase",
    "recovered_head_sha_after_ref_gone",
    "remote_slug",
    "remote_url",
    "rev_count",
    "run",
    "run_strict",
    "soft_reset",
    "status_porcelain",
    "status_porcelain_strict",
    "unsynced_commits",
    "worktree_add",
    "worktree_add_at_ref",
    "worktree_for_branch",
    "worktree_move",
    "worktree_remove",
]


class GitRepo:
    """OOP wrapper — encapsulates repo path, delegates to module-level functions.

    The module-level functions (re-exported above) remain the canonical
    implementation so that ``patch.object(git_mod, "run", ...)`` in tests
    intercepts all call paths.
    """

    def __init__(self, path: str = ".") -> None:
        self.path = path

    def merge_base(self, target: str = "origin/main") -> str:
        return merge_base(self.path, target)

    def rev_count(self, range_spec: str = "") -> int:
        return rev_count(self.path, range_spec)

    def log_oneline(self, range_spec: str = "") -> str:
        return log_oneline(self.path, range_spec)

    def unsynced_commits(self, branch: str, target: str = "origin/main") -> list[str]:
        return unsynced_commits(self.path, branch, target)

    def status_porcelain(self) -> str:
        return status_porcelain(self.path)

    def soft_reset(self, target: str = "") -> None:
        soft_reset(self.path, target)

    def commit(self, message: str = "") -> None:
        commit(self.path, message)

    def fetch(self, remote: str = "origin", ref: str = "") -> None:
        fetch(self.path, remote, ref)

    def rebase(self, target: str = "") -> None:
        rebase(self.path, target)

    def worktree_remove(self, path: str = "") -> bool:
        return worktree_remove(self.path, path)

    def branch_delete(self, branch: str = "") -> bool:
        return branch_delete(self.path, branch)

    def pull_ff_only(self) -> bool:
        return pull_ff_only(self.path)

    def push(self, remote: str = "origin", branch: str = "") -> None:
        push(self.path, remote, branch)

    def default_branch(self) -> str:
        return default_branch(self.path)

    def branch_merged(self, branch: str, target: str = "origin/main") -> bool:
        return branch_merged(self.path, branch, target)

    def current_branch(self) -> str:
        return current_branch(self.path)

    def head_sha(self) -> str:
        return head_sha(self.path)

    def worktree_add_at_ref(self, path: str, ref: str) -> bool:
        return worktree_add_at_ref(self.path, path, ref)

    def remote_url(self, remote: str = "origin") -> str:
        return remote_url(self.path, remote)

    def remote_slug(self, remote: str = "origin") -> str:
        return remote_slug(self.path, remote)

    def config_value(self, key: str = "") -> str:
        return config_value(self.path, key)

    def last_commit_message(self) -> tuple[str, str]:
        return last_commit_message(self.path)

    def commit_messages(self, range_spec: str = "") -> list[str]:
        return commit_messages(self.path, range_spec)

    def worktree_add(self, path: str, branch: str, *, create_branch: bool = True) -> bool:
        return worktree_add(self.path, path, branch, create_branch=create_branch)
