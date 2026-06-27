"""Tests for WorktreeProvisioner — composed runner for the start transition.

Stage 3 of #140: ``Ticket.start()`` becomes a thin transition that enqueues
the heavy I/O (git worktree creation, Worktree DB rows) onto a ``@task``
worker. The worker runs ``WorktreeProvisioner`` and on success schedules
the coding task.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.runners import WorktreeProvisioner
from teatree.utils import git
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestWorktreeProvisioner(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def _scoped_ticket(self, repos: list[str], *, branch: str = "ac-repo-77-x") -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/77",
            repos=repos,
            extra={"branch": branch, "description": "x"},
        )

    def _patch_workspace_dir(self) -> Any:
        return patch("teatree.core.runners.provision._workspace_dir", return_value=self.workspace)

    def test_returns_failure_when_no_repos(self) -> None:
        ticket = self._scoped_ticket(repos=[])

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is False
        assert "no repos" in result.detail.lower()

    def test_creates_worktree_rows_and_git_worktrees(self) -> None:
        repo_dir = self.workspace / "repo-a"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["repo-a"], branch="ac-repo-a-77-x")

        created_paths: list[str] = []

        def fake_worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            del repo, branch, create_branch
            Path(path).mkdir(parents=True, exist_ok=True)
            created_paths.append(path)
            return True

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is True
        wt_path = self.workspace / "ac-repo-a-77-x" / "repo-a"
        assert str(wt_path) in created_paths

        worktrees = list(Worktree.objects.filter(ticket=ticket))
        assert len(worktrees) == 1
        assert worktrees[0].repo_path == "repo-a"
        assert worktrees[0].branch == "ac-repo-a-77-x"
        assert (worktrees[0].extra or {}).get("worktree_path") == str(wt_path)

    def test_idempotent_when_worktree_already_exists(self) -> None:
        repo_dir = self.workspace / "repo-a"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["repo-a"], branch="ac-repo-a-77-x")
        ticket_dir = self.workspace / "ac-repo-a-77-x"
        ticket_dir.mkdir()
        existing_path = ticket_dir / "repo-a"
        existing_path.mkdir()
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="repo-a",
            branch="ac-repo-a-77-x",
            extra={"worktree_path": str(existing_path)},
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add") as worktree_add,
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is True
        worktree_add.assert_not_called()
        assert Worktree.objects.filter(ticket=ticket).count() == 1

    def test_returns_failure_when_worktree_add_fails(self) -> None:
        repo_dir = self.workspace / "repo-a"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["repo-a"], branch="ac-repo-a-77-x")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", return_value=False),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is False
        assert "repo-a" in result.detail
        assert Worktree.objects.filter(ticket=ticket, repo_path="repo-a").count() == 0

    def test_returns_failure_when_no_clone_found_anywhere(self) -> None:
        not_a_repo = self.workspace / "no-git"
        not_a_repo.mkdir()
        ticket = self._scoped_ticket(repos=["no-git"], branch="ac-no-git-77-x")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add") as worktree_add,
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is False
        assert "no-git" in result.detail
        worktree_add.assert_not_called()
        assert Worktree.objects.filter(ticket=ticket, repo_path="no-git").count() == 0

    def test_finds_clone_under_namespaced_subdir(self) -> None:
        namespaced = self.workspace / "souliane" / "teatree"
        namespaced.mkdir(parents=True)
        (namespaced / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["teatree"], branch="ac-teatree-491-x")

        captured: dict[str, str] = {}

        def fake_worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            del branch, create_branch
            captured["source"] = repo
            captured["dest"] = path
            Path(path).mkdir(parents=True, exist_ok=True)
            return True

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is True
        assert captured["source"] == str(namespaced)
        assert captured["dest"] == str(self.workspace / "ac-teatree-491-x" / "teatree")
        wt = Worktree.objects.get(ticket=ticket, repo_path="teatree")
        assert (wt.extra or {}).get("worktree_path") == captured["dest"]
        assert (wt.extra or {}).get("clone_path") == str(namespaced)

    def test_warns_when_multiple_clones_match_basename(self) -> None:
        first = self.workspace / "alpha" / "teatree"
        second = self.workspace / "zeta" / "teatree"
        for clone in (first, second):
            clone.mkdir(parents=True)
            (clone / ".git").mkdir()
        ticket = self._scoped_ticket(repos=["teatree"], branch="ac-teatree-491-multi")

        captured: dict[str, str] = {}

        def fake_worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            del branch, create_branch
            captured["source"] = repo
            Path(path).mkdir(parents=True, exist_ok=True)
            return True

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
            self.assertLogs("teatree.core.clone_paths", level="WARNING") as cm,
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is True
        assert captured["source"] == str(first)
        assert any("Multiple clones match" in msg for msg in cm.output)

    def test_returns_failure_when_branch_missing_from_extra(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/79",
            repos=["repo-a"],
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
        ):
            result = WorktreeProvisioner(ticket).run()

        assert result.ok is False
        assert "branch" in result.detail.lower()


class TestWorktreeProvisionerPerRepoBranches(TestCase):
    """#33: a ticket whose repos live on DIFFERENT branches.

    A ``ticket.extra['branches']`` map (repo → branch) lets each repo
    provision on its own branch while all repos still land as SIBLINGS in
    the ONE ticket dir (``extra['branch']``). Repos not listed in the map
    fall back to ``extra['branch']``. Single-branch tickets (no map) are
    unchanged. This unblocks composing split per-repo branches into one
    e2e/workspace-ticket stack.
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def _patch_workspace_dir(self) -> Any:
        return patch("teatree.core.runners.provision._workspace_dir", return_value=self.workspace)

    def _make_clones(self, *repos: str) -> None:
        for repo in repos:
            repo_dir = self.workspace / repo
            repo_dir.mkdir()
            (repo_dir / ".git").mkdir()

    def _run_capturing_branches(self, ticket: Ticket) -> tuple[Any, dict[str, str], list[str]]:
        """Run the provisioner, capturing the branch passed to each ``git worktree add``."""
        branch_by_dest: dict[str, str] = {}
        created_paths: list[str] = []

        def fake_worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            del repo, create_branch
            branch_by_dest[path] = branch
            created_paths.append(path)
            Path(path).mkdir(parents=True, exist_ok=True)
            return True

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
        ):
            result = WorktreeProvisioner(ticket).run()
        return result, branch_by_dest, created_paths

    def test_multi_branch_repos_provision_as_siblings_on_their_branches(self) -> None:
        # The #8099 shape: two repos on two different fix branches, composed
        # into ONE ticket dir as siblings.
        self._make_clones("repo-a", "repo-b")
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/8099",
            repos=["repo-a", "repo-b"],
            extra={
                "branch": "8099-child-allowance",
                "branches": {
                    "repo-a": "fix/8099-child-allowance-resource",
                    "repo-b": "fix/8099-child-allowance-document-translations",
                },
                "description": "x",
            },
        )

        result, branch_by_dest, _ = self._run_capturing_branches(ticket)

        assert result.ok is True, result.detail

        ticket_dir = self.workspace / "8099-child-allowance"
        path_a = str(ticket_dir / "repo-a")
        path_b = str(ticket_dir / "repo-b")

        # Both repos land as SIBLINGS in the ONE ticket dir.
        assert Path(path_a).parent == ticket_dir
        assert Path(path_b).parent == ticket_dir

        # Each repo is provisioned on ITS OWN branch from the map.
        assert branch_by_dest[path_a] == "fix/8099-child-allowance-resource"
        assert branch_by_dest[path_b] == "fix/8099-child-allowance-document-translations"

        # The Worktree rows record the per-repo branch, not the ticket-dir name.
        wt_a = Worktree.objects.get(ticket=ticket, repo_path="repo-a")
        wt_b = Worktree.objects.get(ticket=ticket, repo_path="repo-b")
        assert wt_a.branch == "fix/8099-child-allowance-resource"
        assert wt_b.branch == "fix/8099-child-allowance-document-translations"
        assert (wt_a.extra or {}).get("worktree_path") == path_a
        assert (wt_b.extra or {}).get("worktree_path") == path_b

    def test_repo_absent_from_map_falls_back_to_ticket_branch(self) -> None:
        # The #1038 shape: one repo on a feature branch, a sibling not listed
        # in the map falls back to the shared ticket branch.
        self._make_clones("repo-a", "repo-b")
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/1038",
            repos=["repo-a", "repo-b"],
            extra={
                "branch": "1038-multi-repo",
                "branches": {"repo-a": "fix/1038-special"},
                "description": "x",
            },
        )

        result, branch_by_dest, _ = self._run_capturing_branches(ticket)

        assert result.ok is True, result.detail
        ticket_dir = self.workspace / "1038-multi-repo"
        path_a = str(ticket_dir / "repo-a")
        path_b = str(ticket_dir / "repo-b")

        assert branch_by_dest[path_a] == "fix/1038-special"
        assert branch_by_dest[path_b] == "1038-multi-repo"

        wt_b = Worktree.objects.get(ticket=ticket, repo_path="repo-b")
        assert wt_b.branch == "1038-multi-repo"

    def test_single_branch_ticket_unchanged(self) -> None:
        # No ``branches`` map → every repo uses ``extra['branch']`` exactly
        # as before (the unchanged single-branch path).
        self._make_clones("repo-a", "repo-b")
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/77",
            repos=["repo-a", "repo-b"],
            extra={"branch": "77-feature", "description": "x"},
        )

        result, branch_by_dest, _ = self._run_capturing_branches(ticket)

        assert result.ok is True, result.detail
        ticket_dir = self.workspace / "77-feature"
        assert branch_by_dest[str(ticket_dir / "repo-a")] == "77-feature"
        assert branch_by_dest[str(ticket_dir / "repo-b")] == "77-feature"

        for repo in ("repo-a", "repo-b"):
            wt = Worktree.objects.get(ticket=ticket, repo_path=repo)
            assert wt.branch == "77-feature"


class TestWorktreeProvisionerCoLocatesAddedRepo(TestCase):
    """A repo ADDED to an in-flight ticket co-locates with the existing worktrees.

    ``workspace ticket --repos`` over a ticket that already has materialised
    worktrees merges the new repo into ``ticket.repos`` for the next provision.
    The added repo must land as a SIBLING of the existing worktrees, even when
    ``extra['branch']`` has drifted from the original ticket-dir name — the
    ``auto:<branch>`` ticket case, where a later ``scope()`` reset
    ``extra['branch']`` to a ``<pk>-ticket`` pk-default. The dir is taken from
    the existing worktrees' shared parent, not blindly from ``extra['branch']``.
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def _patch_workspace_dir(self) -> Any:
        return patch("teatree.core.runners.provision._workspace_dir", return_value=self.workspace)

    def _make_clone(self, repo: str) -> None:
        repo_dir = self.workspace / repo
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

    def _run_capturing_dests(self, ticket: Ticket) -> tuple[Any, list[str]]:
        created_paths: list[str] = []

        def fake_worktree_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            del repo, branch, create_branch
            Path(path).mkdir(parents=True, exist_ok=True)
            created_paths.append(path)
            return True

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            self._patch_workspace_dir(),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
        ):
            result = WorktreeProvisioner(ticket).run()
        return result, created_paths

    def test_added_repo_co_locates_with_existing_worktree_despite_drifted_branch(self) -> None:
        # The exact #8648 footgun: the backend worktree lives in the
        # original branch-named dir, but a later scope() drifted
        # ``extra['branch']`` to a pk-default. Adding the FE must NOT split
        # it into ``<workspace>/<pk>-ticket/``.
        self._make_clone("backend-repo")
        self._make_clone("frontend-repo")
        original_dir = self.workspace / "8648-store-signed-docs"
        backend_wt = original_dir / "backend-repo"
        backend_wt.mkdir(parents=True)

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="auto:8648-store-signed-docs",
            repos=["backend-repo", "frontend-repo"],
            extra={"branch": "23-ticket", "description": "x"},  # drifted pk-default
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend-repo",
            branch="8648-store-signed-docs",
            extra={"worktree_path": str(backend_wt)},
        )

        result, created_paths = self._run_capturing_dests(ticket)

        assert result.ok is True, result.detail
        # The FE co-located as a SIBLING of the existing backend worktree …
        expected_fe = original_dir / "frontend-repo"
        assert str(expected_fe) in created_paths
        # … and was NOT split into the drifted pk-default dir.
        split_fe = self.workspace / "23-ticket" / "frontend-repo"
        assert str(split_fe) not in created_paths
        assert not (self.workspace / "23-ticket").exists()

        fe_wt = Worktree.objects.get(ticket=ticket, repo_path="frontend-repo")
        assert (fe_wt.extra or {}).get("worktree_path") == str(expected_fe)

    def test_first_provision_with_no_existing_worktree_uses_branch_dir(self) -> None:
        # No materialised worktree yet → the normal ``workspace / branch``
        # path is unchanged (the helper returns None, default in force).
        self._make_clone("repo-a")
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/900",
            repos=["repo-a"],
            extra={"branch": "900-feature", "description": "x"},
        )

        result, created_paths = self._run_capturing_dests(ticket)

        assert result.ok is True, result.detail
        assert str(self.workspace / "900-feature" / "repo-a") in created_paths


class TestWorktreeProvisionerStampsScopedIdentity(TestCase):
    """#762 source-fix: public souliane/* worktrees get a local noreply identity.

    A worktree created off a PUBLIC souliane/* clone must get a
    worktree-local noreply git identity (so no path can author with the
    inherited identity). Non-github / private clones must NOT be stamped
    — their legitimate real-identity attribution is untouched.

    #2655: the gate sees the FULL remote URL (host intact), not the
    host-stripped slug, so a GitLab clone whose bare ``owner/repo`` might
    collide with a public github.com repo is never queried — nor stamped
    — as github. The provisioner now passes ``git.remote_url``.
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def _scoped_ticket(self, repos: list[str], *, branch: str) -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/77",
            repos=repos,
            extra={"branch": branch, "description": "x"},
        )

    def _run(
        self, repo: str, branch: str, remote_url: str, *, visibility: str = "PUBLIC"
    ) -> list[tuple[str, str, str]]:
        repo_dir = self.workspace / repo
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        ticket = self._scoped_ticket(repos=[repo], branch=branch)
        stamped: list[tuple[str, str, str]] = []

        def fake_worktree_add(r: str, path: str, b: str, *, create_branch: bool = True) -> bool:
            del r, b, create_branch
            Path(path).mkdir(parents=True, exist_ok=True)
            return True

        def fake_set_local_identity(repo_path: str) -> None:
            from teatree.core.public_identity import canonical_noreply_identity  # noqa: PLC0415

            name, email = canonical_noreply_identity()
            stamped.append((repo_path, name, email))

        # #785: the proactive identity gate is now visibility-based
        # (`gh repo view --json visibility`), not owner-hardcoded — mock
        # the only unstoppable external (the gh subprocess).
        def fake_gh_visibility(cmd: list[str], **_kw: object) -> object:
            del cmd
            return type("R", (), {"stdout": visibility + "\n", "returncode": 0})()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.provision._workspace_dir", return_value=self.workspace),
            patch("teatree.core.runners.provision.git.worktree_add", side_effect=fake_worktree_add),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
            # #2655: the call site now reads the FULL remote URL (host
            # intact); the gate refuses a non-github host before any gh call.
            patch("teatree.core.runners.provision.git.remote_url", return_value=remote_url),
            patch("teatree.core.public_identity.run_allowed_to_fail", side_effect=fake_gh_visibility),
            patch(
                "teatree.core.runners.provision.set_local_noreply_identity",
                side_effect=fake_set_local_identity,
            ),
        ):
            WorktreeProvisioner(ticket).run()
        return stamped

    def test_public_souliane_clone_worktree_is_stamped_noreply(self) -> None:
        from teatree.core.public_identity import is_noreply_email  # noqa: PLC0415

        stamped = self._run("teatree", "ac-teatree-77-x", "git@github.com:souliane/teatree.git", visibility="PUBLIC")

        assert len(stamped) == 1, "public souliane worktree was not identity-stamped (#762 source-fix)"
        wt_path, name, email = stamped[0]
        assert "ac-teatree-77-x" in wt_path
        assert name
        assert is_noreply_email(email), email

    def test_public_non_souliane_clone_worktree_is_stamped_noreply(self) -> None:
        # #785: the exact bug — a PUBLIC repo owned by a non-souliane
        # account must now be stamped (the owner-hardcoded gate missed
        # it, then the reactive hook hard-failed at push).
        from teatree.core.public_identity import is_noreply_email  # noqa: PLC0415

        stamped = self._run(
            "sample-repo",
            "ac-sample-repo-77-x",
            "git@github.com:octo-contrib/sample-repo.git",
            visibility="PUBLIC",
        )

        assert len(stamped) == 1, "public non-souliane worktree was not identity-stamped (#785)"
        _, _, email = stamped[0]
        assert is_noreply_email(email), email

    def test_github_ssh_alias_host_worktree_is_stamped_noreply(self) -> None:
        # #2655: the souliane/teatree clone on this machine uses an
        # ssh-alias host (``github.com-work``) so the github identity is
        # still recognised and the public souliane noreply is stamped.
        from teatree.core.public_identity import is_noreply_email  # noqa: PLC0415

        stamped = self._run(
            "teatree",
            "ac-teatree-alias-x",
            "git@github.com-work:souliane/teatree.git",
            visibility="PUBLIC",
        )

        assert len(stamped) == 1, "ssh-alias github host worktree was not stamped (#2655)"
        _, _, email = stamped[0]
        assert is_noreply_email(email), email

    def test_private_clone_worktree_is_not_stamped(self) -> None:
        stamped = self._run(
            "internal-svc",
            "ac-internal-svc-77-x",
            "git@github.com:acme-private/internal-svc.git",
            visibility="PRIVATE",
        )

        assert stamped == [], "private clone must NOT be identity-stamped — visibility scope error (#785)"

    def test_gitlab_clone_worktree_keeps_inherited_identity(self) -> None:
        # #2655 — the reported bug class: a GitLab clone
        # (``gitlab.com/<owner>/*``) must NEVER be stamped with the github
        # noreply identity, EVEN IF a public github.com repo happened to
        # exist at the same host-stripped ``owner/repo`` slug. The gh mock
        # answers PUBLIC, but the non-github host short-circuits the gate
        # to False BEFORE any gh call, so the GitLab worktree keeps its
        # inherited (real, deliverable-domain) identity.
        stamped = self._run(
            "widget",
            "2655-widget",
            "git@gitlab.com:acme-eng/widget.git",
            visibility="PUBLIC",
        )

        assert stamped == [], (
            "GitLab worktree was stamped with the github identity — "
            "host-blind slug footgun (#2655). It must keep the inherited "
            "real-domain identity."
        )


class TestWorktreeProvisionerGuardsWrongRepo(TestCase):
    """#2276: provisioning a worktree against the WRONG repo must fail loud.

    When ``ticket.repos`` carries an ``owner/repo`` slug, the source clone
    the resolver lands on must actually be that repo. The clone resolver
    matches by basename, so a SIBLING git repo of the same basename — a
    different ``origin`` — would otherwise be cut silently. The guard
    compares the resolved clone's ``origin`` slug against the expected
    slug before ``git worktree add`` and refuses on a mismatch. Real git
    under ``tmp_path`` (no mock of the slug read).
    """

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def _init_clone(self, path: Path, remote_url: str) -> None:
        path.mkdir(parents=True)
        git.run_strict(repo=str(path), args=["init", "-q"])
        git.run_strict(repo=str(path), args=["remote", "add", "origin", remote_url])

    def _scoped_ticket(self, repos: list[str], *, branch: str) -> Ticket:
        return Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/77",
            repos=repos,
            extra={"branch": branch, "description": "x"},
        )

    def _run(self, repos: list[str], branch: str) -> Any:
        ticket = self._scoped_ticket(repos=repos, branch=branch)
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.provision._workspace_dir", return_value=self.workspace),
            patch("teatree.core.runners.provision.git.pull_ff_only", return_value=True),
            patch("teatree.core.runners.provision.is_public_github_remote", return_value=False),
        ):
            return WorktreeProvisioner(ticket).run()

    def test_matching_slug_proceeds(self) -> None:
        clone = self.workspace / "souliane" / "teatree"
        self._init_clone(clone, "git@github.com:souliane/teatree.git")

        result = self._run(["souliane/teatree"], "ac-teatree-2276-ok")

        assert result.ok is True, result.detail
        wt_path = self.workspace / "ac-teatree-2276-ok" / "teatree"
        assert (wt_path / ".git").exists()
        wt = Worktree.objects.get(ticket__repos=["souliane/teatree"], repo_path="souliane/teatree")
        assert (wt.extra or {}).get("worktree_path") == str(wt_path)

    def test_sibling_repo_with_wrong_origin_raises_loud(self) -> None:
        # The wrong-repo footgun: a SIBLING clone of the same basename whose
        # ``origin`` is a different repo. ``ticket.repos`` says the worktree
        # is for ``souliane/teatree`` but the only ``teatree`` clone on disk
        # points at ``someone-else/teatree``.
        sibling = self.workspace / "someone-else" / "teatree"
        self._init_clone(sibling, "git@github.com:someone-else/teatree.git")

        with pytest.raises(ValueError, match="souliane/teatree") as exc:
            self._run(["souliane/teatree"], "ac-teatree-2276-wrong")

        message = str(exc.value)
        assert "someone-else/teatree" in message
        no_worktree = self.workspace / "ac-teatree-2276-wrong" / "teatree"
        assert not no_worktree.exists()

    def test_bare_repo_name_is_not_guarded(self) -> None:
        # A bare basename carries no canonical slug to compare against, so
        # the guard must not fire — the legitimate ``--repos teatree`` flow
        # (resolver scans for the clone) keeps working regardless of origin.
        clone = self.workspace / "souliane" / "teatree"
        self._init_clone(clone, "git@github.com:anyone/teatree.git")

        result = self._run(["teatree"], "ac-teatree-2276-bare")

        assert result.ok is True, result.detail
        assert (self.workspace / "ac-teatree-2276-bare" / "teatree" / ".git").exists()
