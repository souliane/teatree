"""Tests for the pure main-clone git-command classifier (#2836).

The decision core is overlay-agnostic and filesystem-free: given a Bash command
plus the clone's default/protected branches, it returns a finding only for the
precise blocked set — ``checkout``/``switch`` to a non-default branch, ``reset
--hard``, ``restore``, ``stash pop``/``apply`` — and ``None`` for every allowed
hygiene op (``fetch``, ``pull --ff-only``, ``checkout <default>``, ``worktree
…``) and all read-only git. The hook layer (``hooks/scripts/main_clone_guard``)
supplies the environmental facts; these tests pin the parsing in isolation.
"""

import pytest

from teatree.core.gates.main_clone_guard import deny_reason, edit_finding, find_main_clone_git_mutation

_PROTECTED = frozenset({"main", "master"})


def _blocks(command: str, *, default_branch: str | None = "main") -> bool:
    finding = find_main_clone_git_mutation(command, default_branch=default_branch, protected_branches=_PROTECTED)
    return finding is not None


class TestBlockedMutations:
    @pytest.mark.parametrize(
        "command",
        [
            "git checkout feature",
            "git switch feature",
            "git checkout -b new-feature",
            "git switch -c new-feature",
            "git checkout --detach",
            "git switch --detach",
            "git reset --hard origin/main",
            "git reset --hard HEAD~3",
            "git restore src/app.py",
            "git restore --staged src/app.py",
            "git restore .",
            "git stash pop",
            "git stash apply",
            "git stash apply stash@{2}",
            "git checkout -- src/app.py",
            "git checkout main -- src/app.py",
            "cd subdir && git checkout feature",
            "git -C /some/clone checkout feature",
        ],
    )
    def test_working_tree_mutation_is_blocked(self, command: str) -> None:
        assert _blocks(command) is True

    def test_detached_target_off_default_is_blocked_even_without_origin_head(self) -> None:
        # default_branch=None falls back to the protected set {main, master};
        # a feature target is still off-default → blocked.
        assert _blocks("git checkout feature", default_branch=None) is True


class TestAllowedOperations:
    @pytest.mark.parametrize(
        "command",
        [
            "git checkout main",
            "git switch main",
            "git checkout master",
            "git fetch origin",
            "git pull --ff-only",
            "git pull",
            "git pull --ff-only origin main",
            "git worktree add -b feature ../wt origin/main",
            "git worktree remove ../wt",
            "git worktree prune",
            "git worktree list",
            "git status",
            "git log --oneline -5",
            "git diff origin/main",
            "git show HEAD",
            "git rev-parse HEAD",
            "git reset --soft HEAD~1",
            "git reset HEAD src/app.py",
            "git stash",
            "git stash list",
            "git branch -a",
            "git checkout",
            # git invocation with only a global option and no subcommand.
            "git -C /some/clone",
            "git --no-pager",
            # not a git command at all.
            "ls -la",
        ],
    )
    def test_read_only_and_hygiene_ops_allow(self, command: str) -> None:
        assert _blocks(command) is False

    def test_checkout_default_resolved_from_origin_head_allows(self) -> None:
        # A clone whose default branch is "develop" (not in the static protected
        # set) must still allow checking it out — the resolved default is safe.
        assert (
            find_main_clone_git_mutation(
                "git checkout develop",
                default_branch="develop",
                protected_branches=_PROTECTED,
            )
            is None
        )

    def test_unparseable_command_fails_open(self) -> None:
        assert _blocks("git checkout 'unterminated") is False


class TestDenyReason:
    def test_git_reason_points_to_worktree_remediation(self) -> None:
        finding = find_main_clone_git_mutation(
            "git checkout feature", default_branch="main", protected_branches=_PROTECTED
        )
        assert finding is not None
        reason = deny_reason(finding)
        assert "MAIN CLONE" in reason
        assert "worktree" in reason
        assert "origin/main" in reason
        assert "[main-clone-ok:" in reason

    def test_edit_reason_names_the_path(self) -> None:
        reason = deny_reason(edit_finding("/clones/teatree/src/app.py"))
        assert "/clones/teatree/src/app.py" in reason
        assert "MAIN CLONE" in reason
