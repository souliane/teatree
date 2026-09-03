"""The single-branch rule has to be OBSERVED refusing, not merely written down.

Prose said "one branch, one PR, no second worktree" three times over and the two
repos it governed still ended up with 31 worktrees across 37 local branches — two
of them re-implementing a fix already in flight on the integration branch. These
tests are the difference: each one watches the gate actually refuse a specific way
of creating that second branch, and — just as important — watches it stay out of
the way of the operations that bring a non-compliant repo back into line.

Three properties, in order of how they failed in practice:

+ the creation seams refuse (worktree add, checkout -b, switch -c, branch, push);
+ ordinary work on the pinned branch, and the CLEANUP verbs, are untouched — a
    gate that blocks its own remedy gets switched off;
+ an undeclared repo is completely inert, so this can ship enabled by default.
"""

import pytest

from teatree.core.gates.single_branch_repo_guard import (
    check_branch_admitted,
    deny_reason,
    find_second_branch_creation,
    parse_single_branch_repos,
    resolve_pinned_branch,
)

_PINNED = "chore/fork-bootstrap"
_ENTRIES = [
    f"group/widget-core={_PINNED}",
    "group/widget-adapters=chore/adapters-bootstrap",
]


class TestDeclaration:
    @pytest.mark.parametrize(
        ("entries", "expected"),
        [
            (["a/b=main"], {"a/b": "main"}),
            (["a/b=main", "c/d=dev"], {"a/b": "main", "c/d": "dev"}),
            ([" a/b = main "], {"a/b": "main"}),
            # A malformed entry is dropped rather than raising: this is read on a
            # provisioning path, and one typo must not take out every other repo.
            (["a/b=main", "no-branch", "=orphan", "a/c="], {"a/b": "main"}),
            ([], {}),
        ],
    )
    def test_entries_parse(self, entries: list[str], expected: dict[str, str]) -> None:
        assert parse_single_branch_repos(entries) == expected

    @pytest.mark.parametrize(
        "spelling",
        [
            "group/widget-core",
            "org/group/widget-core",
            "git@example.com:org/group/widget-core.git",
            "https://example.com/org/group/widget-core.git",
            "ssh://git@example.com/org/group/widget-core",
            # The BARE name is what ``Ticket.repos`` carries, so the provisioner
            # seam only ever sees this spelling. Missing it would have left the
            # `t3 worktree provision` half of the gate silently inert.
            "widget-core",
        ],
    )
    def test_every_spelling_of_the_same_repo_resolves(self, spelling: str) -> None:
        """Each producer spells the repo differently; requiring one spelling unpins the rest."""
        assert resolve_pinned_branch(spelling, _ENTRIES) == _PINNED

    @pytest.mark.parametrize("repo", ["org/other-service", "someone/other-repo", ""])
    def test_an_undeclared_repo_is_not_pinned(self, repo: str) -> None:
        assert resolve_pinned_branch(repo, _ENTRIES) == ""

    def test_a_similarly_named_repo_is_not_captured_by_suffix_matching(self) -> None:
        assert resolve_pinned_branch("other/not-widget-core", _ENTRIES) == ""


class TestProvisionSeamRefuses:
    def test_a_second_branch_is_refused(self) -> None:
        finding = check_branch_admitted("feat/side-quest", pinned_branch=_PINNED)

        assert finding is not None
        assert finding.surface == "provision"
        assert finding.target == "feat/side-quest"

    def test_the_pinned_branch_is_admitted(self) -> None:
        assert check_branch_admitted(_PINNED, pinned_branch=_PINNED) is None

    def test_an_unpinned_repo_admits_anything(self) -> None:
        assert check_branch_admitted("feat/side-quest", pinned_branch="") is None


class TestRawGitSeamRefuses:
    @pytest.mark.parametrize(
        ("command", "surface", "target"),
        [
            ("git worktree add -b feat/x /tmp/wt", "worktree", "feat/x"),
            ("git worktree add --detach /tmp/wt origin/main", "worktree", "detached"),
            ("git checkout -b feat/x", "branch", "feat/x"),
            ("git checkout -B feat/x origin/main", "branch", "feat/x"),
            ("git switch -c feat/x", "branch", "feat/x"),
            ("git switch --create=feat/x", "branch", "feat/x"),
            ("git branch feat/x", "branch", "feat/x"),
            ("git push origin feat/x", "push", "feat/x"),
            ("git push origin HEAD:refs/heads/feat/x", "push", "feat/x"),
            # A blocked verb hiding in a compound chain is still a second branch.
            ("cd /repo && git checkout -b feat/x", "branch", "feat/x"),
            ("git status; git worktree add -b feat/x /tmp/wt", "worktree", "feat/x"),
            # A -C redirection into the pinned repo must not bypass the gate.
            ("git -C /repo checkout -b feat/x", "branch", "feat/x"),
        ],
    )
    def test_creation_is_refused(self, command: str, surface: str, target: str) -> None:
        finding = find_second_branch_creation(command, pinned_branch=_PINNED)

        assert finding is not None, command
        assert (finding.surface, finding.target) == (surface, target)

    @pytest.mark.parametrize(
        "command",
        [
            # Ordinary work on the pinned branch.
            "git commit -m 'fix: something'",
            "git fetch origin --prune",
            "git pull --ff-only",
            f"git checkout {_PINNED}",
            f"git switch {_PINNED}",
            f"git push origin {_PINNED}",
            f"git push origin HEAD:refs/heads/{_PINNED}",
            "git push",
            "git log --oneline -5",
            "git status --porcelain",
            "git cherry-pick abc1234",
            # The CLEANUP verbs. Refusing these would make the gate defend the
            # very sprawl it exists to prevent.
            "git worktree list",
            "git worktree remove /tmp/wt",
            "git worktree prune",
            "git branch -D feat/x",
            "git branch --delete feat/x",
            "git branch -a",
            "git branch --list",
            "git branch --show-current",
            # A non-branch ref push is not a second branch.
            "git push origin refs/tags/v1.0.0",
            # Unparsable input fails OPEN rather than blocking what it cannot read.
            'git commit -m "unbalanced',
            "",
        ],
    )
    def test_allowed_commands_are_not_refused(self, command: str) -> None:
        assert find_second_branch_creation(command, pinned_branch=_PINNED) is None, command

    @pytest.mark.parametrize(
        "command",
        ["git worktree add -b feat/x /tmp/wt", "git checkout -b feat/x", "git push origin feat/x"],
    )
    def test_an_unpinned_repo_is_completely_inert(self, command: str) -> None:
        """Shipping enabled by default is only safe if an undeclared repo never fires."""
        assert find_second_branch_creation(command, pinned_branch="") is None


class TestRefspecLessPushPublishesTheCheckedOutBranch:
    """``checkout`` creates nothing, so a pre-existing side branch reaches a bare ``git push``.

    A repo is declared single-branch precisely because it already carries the side
    branches that motivated the declaration, so "on a compliant repo the current
    branch IS the pinned one" is false exactly when the gate matters: check one
    out, ``git push``, and the second MR appears with nothing refused.
    """

    @pytest.mark.parametrize("command", ["git push", "git push origin", "git push -u origin"])
    def test_a_bare_push_from_a_side_branch_is_refused(self, command: str) -> None:
        finding = find_second_branch_creation(command, pinned_branch=_PINNED, current_branch="feat/x")

        assert finding is not None, command
        assert (finding.surface, finding.target) == ("push", "feat/x")

    @pytest.mark.parametrize("command", ["git push", "git push origin", "git push --force-with-lease"])
    def test_a_bare_push_from_the_pinned_branch_allows(self, command: str) -> None:
        assert find_second_branch_creation(command, pinned_branch=_PINNED, current_branch=_PINNED) is None, command

    def test_an_unresolved_current_branch_allows(self) -> None:
        # A detached HEAD or an unreadable repo must never become a false refusal.
        assert find_second_branch_creation("git push", pinned_branch=_PINNED, current_branch="") is None

    def test_an_explicit_pinned_refspec_still_allows_from_a_side_branch(self) -> None:
        # ANTI-VACUOUS: the current branch decides only the refspec-LESS form.
        assert (
            find_second_branch_creation(
                f"git push origin HEAD:{_PINNED}", pinned_branch=_PINNED, current_branch="feat/x"
            )
            is None
        )


class TestDenyMessageNamesTheRule:
    @pytest.mark.parametrize("surface", ["worktree", "branch", "push", "provision"])
    def test_every_surface_names_the_pinned_branch_and_the_way_out(self, surface: str) -> None:
        finding = check_branch_admitted("feat/x", pinned_branch=_PINNED)
        assert finding is not None
        reason = deny_reason(
            type(finding)(surface=surface, target="feat/x"),
            pinned_branch=_PINNED,
            repo="group/widget-core",
        )

        assert "BLOCKED" in reason
        assert _PINNED in reason
        assert "SINGLE-BRANCH" in reason
        assert "group/widget-core" in reason
        # The rule must say how it ENDS, or the next operator works around it.
        assert "single_branch_repos" in reason
        assert "[single-branch-ok:" in reason
