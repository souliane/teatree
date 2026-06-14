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


class TestWorktreeProvisionerStampsScopedIdentity(TestCase):
    """#762 source-fix: public souliane/* worktrees get a local noreply identity.

    A worktree created off a PUBLIC souliane/* clone must get a
    worktree-local noreply git identity (so no path can author with the
    inherited identity). Non-souliane / private clones must NOT be stamped
    — their legitimate real-identity attribution is untouched.
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
            patch("teatree.core.runners.provision.git.remote_slug", return_value=remote_url),
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

        stamped = self._run("teatree", "ac-teatree-77-x", "souliane/teatree", visibility="PUBLIC")

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

        stamped = self._run("sample-repo", "ac-sample-repo-77-x", "octo-contrib/sample-repo", visibility="PUBLIC")

        assert len(stamped) == 1, "public non-souliane worktree was not identity-stamped (#785)"
        _, _, email = stamped[0]
        assert is_noreply_email(email), email

    def test_private_clone_worktree_is_not_stamped(self) -> None:
        stamped = self._run("internal-svc", "ac-internal-svc-77-x", "acme-private/internal-svc", visibility="PRIVATE")

        assert stamped == [], "private clone must NOT be identity-stamped — visibility scope error (#785)"


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
