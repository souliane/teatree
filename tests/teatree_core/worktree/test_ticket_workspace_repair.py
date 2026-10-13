# test-path: cross-cutting
"""The targeted split-workspace recovery (#111 finding 4, real git).

``workspace clean-all`` is the DONE-worktree reaper — it deliberately keeps an
unfinished checkout, so prescribing it for a still-open split ticket repairs
nothing. This is the recovery that actually does: it MOVES each divergent
checkout into the ticket's canonical dir, preserving both an unpushed commit
and an uncommitted change, rather than reaping anything.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.unshipped_restore import restore_bundle
from teatree.core.cleanup.unshipped_work import (
    UNCOMMITTED_SUFFIX,
    bundle_path,
    capture_unshipped_work,
    probe_unshipped_work,
)
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.ticket_workspace import ticket_workspace_dirs
from teatree.core.worktree.ticket_workspace_repair import repair_split_workspace
from teatree.utils import git

_BRANCH = "111-repair"


class TestRepairSplitWorkspace(TestCase):
    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        home = tmp_path / "home"
        self.home_patch = patch("pathlib.Path.home", return_value=home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.artifact_patch = patch(
            "teatree.core.worktree.ticket_workspace_repair.get_data_dir", return_value=tmp_path / "artifacts"
        )
        self.artifact_patch.start()
        self.addCleanup(self.artifact_patch.stop)
        (tmp_path / "artifacts").mkdir()
        self.workspace = home / "workspace" / "t3-workspaces"
        self.workspace.mkdir(parents=True)

    def _clone(self, repo: str) -> Path:
        # A real, PUSHED origin: unpushed-work detection asks whether a commit is
        # absent from every remote, so a remote-less fixture would make even the
        # base commit look unpushed.
        origin = self.tmp / "origin" / f"{repo}.git"
        git.run_strict(repo=str(self.tmp), args=["init", "-q", "--bare", "-b", "main", str(origin)])
        clone = self.tmp / "clones" / repo
        clone.mkdir(parents=True)
        git.run_strict(repo=str(clone), args=["init", "-q", "-b", "main"])
        git.run_strict(repo=str(clone), args=["config", "user.email", "t@example.com"])
        git.run_strict(repo=str(clone), args=["config", "user.name", "t"])
        git.run_strict(repo=str(clone), args=["remote", "add", "origin", str(origin)])
        (clone / "README.md").write_text("x\n", encoding="utf-8")
        git.run_strict(repo=str(clone), args=["add", "-A"])
        git.run_strict(repo=str(clone), args=["commit", "-q", "-m", "init"])
        git.run_strict(repo=str(clone), args=["push", "-q", "-u", "origin", "main"])
        return clone

    def _add_worktree(self, clone: Path, path: Path, branch: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        git.run_strict(repo=str(clone), args=["worktree", "add", "-q", "-b", branch, str(path)])
        return path

    def _split_ticket(self, *, dirty: bool = True) -> tuple[Ticket, Path, Path]:
        backend = self._add_worktree(self._clone("backend"), self.workspace / _BRANCH / "backend", _BRANCH)
        frontend = self._add_worktree(self._clone("frontend"), self.tmp / "foreign" / "frontend", _BRANCH)
        if dirty:
            (frontend / "work.txt").write_text("original\n", encoding="utf-8")
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url=f"https://example.com/issues/{self.id()}",
            repos=["backend", "frontend"],
            extra={"branch": _BRANCH},
        )
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="backend", branch=_BRANCH, extra={"worktree_path": str(backend)}
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="frontend",
            branch=_BRANCH,
            extra={"worktree_path": str(frontend)},
        )
        return ticket, backend, frontend

    def test_refuses_disallowed_canonical_root_before_any_write(self) -> None:
        ticket, _, frontend = self._split_ticket()
        outside = self.tmp / "outside"
        with patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=outside):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert "outside allowed" in outcome.render()
        assert not outside.exists()
        assert frontend.is_dir()

    def test_refuses_recorded_branch_mismatch_before_remove(self) -> None:
        ticket, _, frontend = self._split_ticket()
        git.run_strict(repo=str(frontend), args=["checkout", "--detach", "-q"])
        with patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert _BRANCH in outcome.render()
        assert "detached" in outcome.render()
        assert frontend.is_dir()

    def test_failed_new_add_recreates_old_checkout_with_uncommitted_work(self) -> None:
        ticket, _, frontend = self._split_ticket()
        Worktree.objects.filter(ticket=ticket, repo_path="frontend").update(overlay="t3-teatree")
        new_path = self.workspace / _BRANCH / "frontend"
        actual_add = git.worktree_add

        def fail_new_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            return False if Path(path) == new_path else actual_add(repo, path, branch, create_branch=create_branch)

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_add", side_effect=fail_new_add),
        ):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert (frontend / "work.txt").read_text(encoding="utf-8") == "original\n"
        assert "t3 teatree workspace restore" in outcome.render()
        assert f"--into {frontend}" in outcome.render()

    def test_failed_recovery_add_names_bundle_without_nonexistent_restore_target(self) -> None:
        ticket, _, frontend = self._split_ticket()
        new_path = self.workspace / _BRANCH / "frontend"
        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_add", return_value=False),
        ):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert not frontend.exists()
        assert not new_path.exists()
        assert "salvage bundle:" in outcome.render()
        assert "--into" not in outcome.render()

    def test_refuses_incomplete_bundle_before_remove(self) -> None:
        ticket, _, frontend = self._split_ticket()
        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.capture_unshipped_work", return_value=None),
        ):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_refuses_partial_bundle_before_remove(self) -> None:
        ticket, _, frontend = self._split_ticket()

        def partial_capture(checkout: Path, *, branch: str, overlay: str, artifact_root: Path) -> object:
            record = capture_unshipped_work(checkout, branch=branch, overlay=overlay, artifact_root=artifact_root)
            assert record is not None
            bundle_path(record.artifact_prefix, UNCOMMITTED_SUFFIX).write_text("partial", encoding="utf-8")
            return record

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.capture_unshipped_work", side_effect=partial_capture),
        ):
            outcome = repair_split_workspace(ticket)
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_refuses_bundle_from_earlier_capture(self) -> None:
        ticket, _, frontend = self._split_ticket()
        old_root = self.tmp / "older-artifacts"
        record = capture_unshipped_work(frontend, branch=_BRANCH, overlay="test", artifact_root=old_root)
        assert record is not None
        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.capture_unshipped_work", return_value=record),
        ):
            outcome = repair_split_workspace(ticket)
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_refuses_same_length_edit_between_reads(self) -> None:
        ticket, _, frontend = self._split_ticket()

        def edited_capture(checkout: Path, *, branch: str, overlay: str, artifact_root: Path) -> object:
            (checkout / "work.txt").write_text("modified\n", encoding="utf-8")
            return capture_unshipped_work(checkout, branch=branch, overlay=overlay, artifact_root=artifact_root)

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.capture_unshipped_work", side_effect=edited_capture),
        ):
            outcome = repair_split_workspace(ticket)
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_refuses_work_added_after_clean_first_read(self) -> None:
        ticket, _, frontend = self._split_ticket(dirty=False)
        calls = 0

        def late_file(checkout: Path) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                (checkout / "late.txt").write_text("late\n", encoding="utf-8")
            return probe_unshipped_work(checkout)

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.probe_unshipped_work", side_effect=late_file),
        ):
            outcome = repair_split_workspace(ticket)
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_moves_clean_checkout_without_bundle(self) -> None:
        ticket, _, frontend = self._split_ticket(dirty=False)
        with patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace):
            outcome = repair_split_workspace(ticket)
        assert outcome.ok, outcome.render()
        assert not frontend.exists()
        assert (self.workspace / _BRANCH / "frontend").is_dir()
        assert list((self.tmp / "artifacts").iterdir()) == []

    def test_refuses_unwritable_bundle_root(self) -> None:
        ticket, _, frontend = self._split_ticket()
        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.get_data_dir", side_effect=OSError("read only")),
        ):
            outcome = repair_split_workspace(ticket)
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_failed_reapply_names_fresh_restore_even_after_prior_capture(self) -> None:
        ticket, _, frontend = self._split_ticket()
        prior = capture_unshipped_work(frontend, branch=_BRANCH, overlay="test", artifact_root=self.tmp / "older")
        assert prior is not None
        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch(
                "teatree.core.worktree.ticket_workspace_repair._apply_uncommitted_delta",
                return_value="apply failed",
            ),
        ):
            outcome = repair_split_workspace(ticket)
        moved = self.workspace / _BRANCH / "frontend"
        assert not outcome.ok
        assert "previous salvage" in outcome.render()
        prefix = outcome.render().split("workspace restore ", 1)[1].split(" --into", 1)[0]
        assert prefix != prior.artifact_prefix
        assert restore_bundle(prefix, moved).ok
        assert (moved / "work.txt").read_text(encoding="utf-8") == "original\n"

    def test_repairs_a_split_unfinished_ticket_preserving_unpushed_and_uncommitted_work(self) -> None:
        backend_clone = self._clone("backend")
        frontend_clone = self._clone("frontend")

        canonical = self.workspace / _BRANCH
        backend = self._add_worktree(backend_clone, canonical / "backend", _BRANCH)

        foreign_root = self.tmp / "foreign-root"
        frontend = self._add_worktree(frontend_clone, foreign_root / "frontend", _BRANCH)
        # Unfinished, real work: one unpushed commit AND one uncommitted change.
        (frontend / "feature.py").write_text("unpushed = True\n", encoding="utf-8")
        git.run_strict(repo=str(frontend), args=["add", "-A"])
        git.run_strict(repo=str(frontend), args=["commit", "-q", "-m", "unpushed work"])
        (frontend / "wip.py").write_text("uncommitted = True\n", encoding="utf-8")

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/111-repair",
            repos=["backend", "frontend"],
            extra={"branch": _BRANCH},
        )
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="backend", branch=_BRANCH, extra={"worktree_path": str(backend)}
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="frontend",
            branch=_BRANCH,
            extra={"worktree_path": str(frontend)},
        )
        assert len(ticket_workspace_dirs(ticket)) == 2, "fixture must start genuinely split"

        with patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace):
            outcome = repair_split_workspace(ticket)

        assert outcome.ok, outcome.render()
        assert outcome.moved == ["frontend"]
        assert outcome.kept == ["backend"]
        assert len(ticket_workspace_dirs(ticket)) == 1, "the split must be actually repaired"

        moved_row = Worktree.objects.get(ticket=ticket, repo_path="frontend")
        new_path = Path(moved_row.extra["worktree_path"])
        assert new_path.parent == canonical, "frontend must have moved INTO the canonical dir"
        assert (new_path / "feature.py").read_text(encoding="utf-8") == "unpushed = True\n", "unpushed commit lost"
        assert (new_path / "wip.py").read_text(encoding="utf-8") == "uncommitted = True\n", "uncommitted work lost"
        assert git.current_branch(str(new_path)) == _BRANCH
        assert not frontend.exists(), "the old foreign-root checkout must be gone, not duplicated"

    def test_a_ticket_with_one_workspace_dir_is_a_no_op(self) -> None:
        backend_clone = self._clone("backend")
        canonical = self.workspace / _BRANCH
        backend = self._add_worktree(backend_clone, canonical / "backend", _BRANCH)
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/111-noop",
            repos=["backend"],
            extra={"branch": _BRANCH},
        )
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="backend", branch=_BRANCH, extra={"worktree_path": str(backend)}
        )

        with patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace):
            outcome = repair_split_workspace(ticket)

        assert outcome.ok
        assert outcome.moved == []
        assert outcome.kept == []
