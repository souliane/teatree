# test-path: cross-cutting
"""The targeted split-workspace recovery (#111 finding 4, real git).

``workspace clean-all`` is the DONE-worktree reaper — it deliberately keeps an
unfinished checkout, so prescribing it for a still-open split ticket repairs
nothing. This is the recovery that actually does: it MOVES each divergent
checkout into the ticket's canonical dir, preserving both an unpushed commit
and an uncommitted change, rather than reaping anything.
"""

import errno
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup import unshipped_work as unshipped_work_mod
from teatree.core.cleanup.unshipped_restore import restore_bundle
from teatree.core.cleanup.unshipped_work import (
    UNCOMMITTED_SUFFIX,
    bundle_path,
    capture_unshipped_work,
    probe_unshipped_work,
    write_salvage_bundle,
)
from teatree.core.models import Ticket, UnshippedWorkRecord, Worktree
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
        assert "workspace restore" not in outcome.render()

    def test_failed_new_add_and_failed_old_reapply_names_bundle(self) -> None:
        ticket, _, frontend = self._split_ticket()
        new_path = self.workspace / _BRANCH / "frontend"
        actual_add = git.worktree_add

        def fail_new_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            return False if Path(path) == new_path else actual_add(repo, path, branch, create_branch=create_branch)

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_add", side_effect=fail_new_add),
            patch("teatree.core.worktree.ticket_workspace_repair._apply_uncommitted_delta", return_value="failed"),
        ):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert frontend.is_dir()
        assert f"--into {frontend}" in outcome.render()
        prefix = outcome.render().split("workspace restore ", 1)[1].split(" --into", 1)[0]
        assert restore_bundle(prefix, frontend).ok
        assert (frontend / "work.txt").read_text(encoding="utf-8") == "original\n"

    def test_failed_new_add_for_clean_checkout_has_no_restore_hint(self) -> None:
        ticket, _, frontend = self._split_ticket(dirty=False)
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
        assert frontend.is_dir()
        assert "workspace restore" not in outcome.render()

    def test_uncreatable_target_parent_moves_nothing_and_offers_no_restore(self) -> None:
        ticket, _, frontend = self._split_ticket()
        canonical = self.workspace / _BRANCH
        actual_mkdir = Path.mkdir

        def refuse_canonical(path: Path, *args: object, **kwargs: object) -> None:
            if path == canonical:
                raise OSError(errno.EROFS, str(path))
            actual_mkdir(path, *args, **kwargs)

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch.object(Path, "mkdir", autospec=True, side_effect=refuse_canonical),
        ):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert "nothing moved" in outcome.render()
        assert "workspace restore" not in outcome.render()
        assert (frontend / "work.txt").read_text(encoding="utf-8") == "original\n"

    def test_failed_remove_names_the_bundle_without_a_restore_command(self) -> None:
        ticket, _, frontend = self._split_ticket()
        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_remove", return_value=False),
        ):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert "workspace restore" not in outcome.render()
        prefix = outcome.render().split("salvage bundle: ", 1)[1].split(";", 1)[0].strip()
        assert bundle_path(prefix, UNCOMMITTED_SUFFIX).is_file()
        assert frontend.is_dir()

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
            patch("teatree.core.worktree.ticket_workspace_repair.write_salvage_bundle", return_value=None),
        ):
            outcome = repair_split_workspace(ticket)
        assert not outcome.ok
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_refuses_partial_bundle_before_remove(self) -> None:
        ticket, _, frontend = self._split_ticket()

        def partial_write(checkout: Path, *, branch: str, root: Path) -> Path | None:
            prefix = write_salvage_bundle(checkout, branch=branch, root=root)
            assert prefix is not None
            bundle_path(str(prefix), UNCOMMITTED_SUFFIX).write_text("partial", encoding="utf-8")
            return prefix

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.write_salvage_bundle", side_effect=partial_write),
        ):
            outcome = repair_split_workspace(ticket)
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_refuses_bundle_from_earlier_capture(self) -> None:
        ticket, _, frontend = self._split_ticket()
        earlier = write_salvage_bundle(frontend, branch=_BRANCH, root=self.tmp / "older-artifacts")
        assert earlier is not None
        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.write_salvage_bundle", return_value=earlier),
        ):
            outcome = repair_split_workspace(ticket)
        assert "nothing moved" in outcome.render()
        assert "restore" not in outcome.render()
        assert frontend.is_dir()

    def test_refuses_same_length_edit_between_reads(self) -> None:
        ticket, _, frontend = self._split_ticket()

        def edited_write(checkout: Path, *, branch: str, root: Path) -> Path | None:
            (checkout / "work.txt").write_text("modified\n", encoding="utf-8")
            return write_salvage_bundle(checkout, branch=branch, root=root)

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.write_salvage_bundle", side_effect=edited_write),
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

    def test_bundle_retirement_failure_is_reported_after_move(self) -> None:
        ticket, _, frontend = self._split_ticket()
        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("pathlib.Path.rmdir", side_effect=OSError("busy")),
        ):
            outcome = repair_split_workspace(ticket)
        moved = self.workspace / _BRANCH / "frontend"
        assert not outcome.ok
        assert "could not remove" in outcome.render()
        assert (moved / "work.txt").read_text(encoding="utf-8") == "original\n"
        assert not UnshippedWorkRecord.objects.filter(checkout_path=str(frontend)).exists()

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
        assert Worktree.objects.get(ticket=ticket, repo_path="frontend").extra["worktree_path"] == str(moved)
        prefix = outcome.render().split("workspace restore ", 1)[1].split(" --into", 1)[0]
        assert prefix != prior.artifact_prefix
        assert restore_bundle(prefix, moved).ok
        assert (moved / "work.txt").read_text(encoding="utf-8") == "original\n"

    def test_restore_hint_survives_a_later_default_root_sweep_of_the_checkout(self) -> None:
        ticket, _, frontend = self._split_ticket()
        new_path = self.workspace / _BRANCH / "frontend"
        actual_add = git.worktree_add

        def fail_new_add(repo: str, path: str, branch: str, *, create_branch: bool = True) -> bool:
            return False if Path(path) == new_path else actual_add(repo, path, branch, create_branch=create_branch)

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_add", side_effect=fail_new_add),
            patch("teatree.core.worktree.ticket_workspace_repair._apply_uncommitted_delta", return_value="failed"),
        ):
            outcome = repair_split_workspace(ticket)
        prefix = outcome.render().split("workspace restore ", 1)[1].split(" --into", 1)[0]
        (frontend / "later.txt").write_text("later\n", encoding="utf-8")
        with patch("teatree.core.cleanup.unshipped_work.get_data_dir", return_value=self.tmp / "artifacts"):
            assert capture_unshipped_work(frontend, branch=_BRANCH, overlay="test") is not None
        (frontend / "later.txt").unlink()

        assert restore_bundle(prefix, frontend).ok
        assert (frontend / "work.txt").read_text(encoding="utf-8") == "original\n"

    def test_a_sweep_capture_during_the_repair_keeps_its_salvage_record(self) -> None:
        ticket, _, frontend = self._split_ticket()
        prior = capture_unshipped_work(frontend, branch=_BRANCH, overlay="test", artifact_root=self.tmp / "older")
        assert prior is not None
        sweep_root = self.tmp / "sweep"
        actual_write = unshipped_work_mod._write_bundle

        def sweep_during_write(prefix: Path, checkout: Path, branch: str, work: object) -> None:
            actual_write(prefix, checkout, branch, work)
            if prefix.parent != sweep_root:
                capture_unshipped_work(checkout, branch=branch, overlay="test", artifact_root=sweep_root)

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch.object(unshipped_work_mod, "_write_bundle", side_effect=sweep_during_write),
        ):
            outcome = repair_split_workspace(ticket)

        assert outcome.ok, outcome.render()
        record = UnshippedWorkRecord.objects.get(checkout_path=str(frontend))
        assert Path(record.artifact_prefix).parent == sweep_root

    def test_success_keeps_an_earlier_salvage_record_and_bundle(self) -> None:
        ticket, _, frontend = self._split_ticket()
        prior = capture_unshipped_work(frontend, branch=_BRANCH, overlay="test", artifact_root=self.tmp / "older")
        assert prior is not None
        prefix = prior.artifact_prefix
        assert bundle_path(prefix, UNCOMMITTED_SUFFIX).is_file()

        with patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace):
            outcome = repair_split_workspace(ticket)

        assert outcome.ok, outcome.render()
        prior.refresh_from_db()
        assert prior.artifact_prefix == prefix
        assert bundle_path(prefix, UNCOMMITTED_SUFFIX).is_file()

    def test_keeps_a_split_checkout_with_commit_on_no_remote(self) -> None:
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

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_remove") as remove,
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_add") as add,
        ):
            outcome = repair_split_workspace(ticket)

        assert not outcome.ok
        assert "no live remote ref" in outcome.render()
        assert str(frontend) in outcome.render()
        assert (frontend / "feature.py").read_text(encoding="utf-8") == "unpushed = True\n"
        assert (frontend / "wip.py").read_text(encoding="utf-8") == "uncommitted = True\n"
        remove.assert_not_called()
        add.assert_not_called()

    def test_keeps_ignored_file_before_split_move(self) -> None:
        ticket, _, frontend = self._split_ticket(dirty=False)
        (frontend / ".gitignore").write_text("notes.local\n", encoding="utf-8")
        git.run_strict(repo=str(frontend), args=["add", ".gitignore"])
        git.run_strict(repo=str(frontend), args=["commit", "-q", "-m", "ignore notes"])
        git.run_strict(repo=str(frontend), args=["push", "-q", "origin", _BRANCH])
        (frontend / "notes.local").write_text("retain\n", encoding="utf-8")

        with (
            patch("teatree.core.worktree.ticket_workspace_repair.worktree_root", return_value=self.workspace),
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_remove") as remove,
            patch("teatree.core.worktree.ticket_workspace_repair.git.worktree_add") as add,
        ):
            outcome = repair_split_workspace(ticket)

        assert not outcome.ok
        assert "notes.local" in outcome.render()
        assert (frontend / "notes.local").read_text(encoding="utf-8") == "retain\n"
        remove.assert_not_called()
        add.assert_not_called()

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
