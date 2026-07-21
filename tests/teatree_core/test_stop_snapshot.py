"""Continuous stop-snapshotter shared implementation (souliane/teatree#2564 / PR-20).

Real ``git init`` + linked worktrees under ``tmp_path`` exercise the at-risk
recovery path; Django rows exercise the resume-plan render. The headline
acceptance test simulates a ``/tmp`` worktree whose working dir is wiped and
proves the uncommitted work is still recoverable from the shared ``.git``.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core import stop_snapshot
from teatree.core.models import DeferredQuestion, LoopPreset, LoopPresetOverride, PullRequest, Ticket


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],  # noqa: S607 — git on PATH; fixed test argv
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _init_main_clone(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(root, "config", "user.name", "Tester")
    (root / "a.txt").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")


def _add_worktree(main: Path, wt: Path, branch: str = "feat") -> None:
    _git(main, "worktree", "add", "-b", branch, str(wt))


class TestResumeDir:
    def test_default_under_xdg_state(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        assert stop_snapshot.resume_dir() == tmp_path / "state" / "teatree" / "resume"

    def test_explicit_base_wins(self, tmp_path: Path) -> None:
        assert stop_snapshot.resume_dir(tmp_path / "r") == tmp_path / "r"


class TestTodoMirror:
    def test_writes_pending_todos(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            "teatree.core.harness_todos.read_harness_todos",
            lambda _sid: [("in_progress", "wire the slot"), ("pending", "add the CLI")],
        )
        path = stop_snapshot.write_todo_mirror("sess-1", base=tmp_path)
        assert path is not None
        body = path.read_text()
        assert "wire the slot" in body
        assert "add the CLI" in body

    def test_empty_todos_still_writes_a_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            "teatree.core.harness_todos.read_harness_todos",
            lambda _sid: [],
        )
        path = stop_snapshot.write_todo_mirror("sess-1", base=tmp_path)
        assert path is not None
        assert path.exists()


class TestResumePlan(TestCase):
    """The resume-plan render touches the DB (PullRequest / DeferredQuestion)."""

    def test_includes_prs_questions_and_mode(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        PullRequest.objects.create(ticket=ticket, url="https://x/pr/9", repo="souliane/teatree", iid="9")
        DeferredQuestion.objects.create(question="Which branch — main or dev?")
        # A real away-class mode override — the resume plan reads the merged mode.
        LoopPreset.objects.update_or_create(
            name="offline", defaults={"entries": {}, "defers_questions": True, "pauses_self_pump": True}
        )
        LoopPresetOverride.objects.set_override("offline")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = stop_snapshot.write_resume_plan("sess-1", str(tmp), base=tmp).read_text()
        assert "souliane/teatree #9" in body
        assert "Which branch" in body
        assert "mode: offline" in body
        assert "defers questions: True" in body

    def test_merged_prs_excluded(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        pr = PullRequest.objects.create(ticket=ticket, url="https://x/pr/10", repo="souliane/teatree", iid="10")
        pr.state = PullRequest.State.MERGED
        pr.save()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            body = stop_snapshot.write_resume_plan("sess-2", str(tmp), base=tmp).read_text()
        assert "#10" not in body


class TestAtRiskWorktree:
    def test_recoverable_after_working_dir_deleted(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        _init_main_clone(main)
        wt = tmp_path / "tmp-like" / "wt"
        _add_worktree(main, wt)
        (wt / "a.txt").write_text("WORK IN PROGRESS\n")  # uncommitted tracked edit
        (wt / "new.txt").write_text("brand new file\n")  # untracked new file

        result = stop_snapshot.handle_at_risk_worktree(str(wt))
        assert result is not None
        ref = result.recovery_ref

        shutil.rmtree(wt)
        _git(main, "worktree", "prune")

        # Objects + ref survive in the shared .git under the (kept) main clone —
        # both the tracked edit and the untracked new file are recoverable.
        assert "WORK IN PROGRESS" in _git(main, "show", f"{ref}:a.txt")
        assert "brand new file" in _git(main, "show", f"{ref}:new.txt")

    def test_clean_tree_is_noop(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        _init_main_clone(main)
        wt = tmp_path / "wt"
        _add_worktree(main, wt)
        assert stop_snapshot.handle_at_risk_worktree(str(wt)) is None

    def test_does_not_mutate_the_real_index(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        _init_main_clone(main)
        wt = tmp_path / "wt"
        _add_worktree(main, wt)
        (wt / "a.txt").write_text("edited\n")
        stop_snapshot.handle_at_risk_worktree(str(wt))
        # The real index is untouched — a.txt is still an unstaged modification.
        assert _git(wt, "diff", "--cached", "--name-only") == ""
        assert "a.txt" in _git(wt, "status", "--porcelain")

    def test_idempotent_single_ref_no_branch_commits(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        _init_main_clone(main)
        wt = tmp_path / "wt"
        _add_worktree(main, wt)
        (wt / "a.txt").write_text("v1\n")
        first = stop_snapshot.handle_at_risk_worktree(str(wt))
        assert first is not None
        head_before = _git(wt, "rev-parse", "HEAD")
        second = stop_snapshot.handle_at_risk_worktree(str(wt))
        assert second is not None
        assert second.recovery_ref == first.recovery_ref
        # The branch HEAD never advanced — no chore: commit piled onto the branch.
        assert _git(wt, "rev-parse", "HEAD") == head_before
        # Exactly one resume ref for this worktree.
        refs = _git(main, "for-each-ref", "--format=%(refname)", "refs/t3-resume/").splitlines()
        assert len(refs) == 1


class TestFailureBranches:
    def test_blank_session_writes_no_todo_mirror(self, tmp_path: Path) -> None:
        assert stop_snapshot.write_todo_mirror("", base=tmp_path) is None

    def test_git_helper_returns_empty_on_oserror(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def _raise(*_a: object, **_k: object) -> object:
            raise OSError

        monkeypatch.setattr(stop_snapshot, "run_allowed_to_fail", _raise)
        assert stop_snapshot._git(tmp_path, "status") == ""

    def _at_risk_repo(self, tmp_path: Path) -> Path:
        main = tmp_path / "main"
        _init_main_clone(main)
        wt = tmp_path / "wt"
        _add_worktree(main, wt)
        (wt / "a.txt").write_text("dirty\n")
        return wt

    def _fail_git_verb(self, verb: str, *, ref_only: bool = False):
        real = stop_snapshot._git

        def wrapper(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
            if args and args[0] == verb and (not ref_only or args[-1].startswith("refs/t3-resume/")):
                return ""
            return real(repo, *args, env=env)

        return wrapper

    def test_none_when_write_tree_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        wt = self._at_risk_repo(tmp_path)
        monkeypatch.setattr(stop_snapshot, "_git", self._fail_git_verb("write-tree"))
        assert stop_snapshot.handle_at_risk_worktree(str(wt)) is None

    def test_none_when_commit_tree_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        wt = self._at_risk_repo(tmp_path)
        monkeypatch.setattr(stop_snapshot, "_git", self._fail_git_verb("commit-tree"))
        assert stop_snapshot.handle_at_risk_worktree(str(wt)) is None

    def test_none_when_ref_verify_mismatches(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        wt = self._at_risk_repo(tmp_path)
        monkeypatch.setattr(stop_snapshot, "_git", self._fail_git_verb("rev-parse", ref_only=True))
        assert stop_snapshot.handle_at_risk_worktree(str(wt)) is None


class TestPrepareStop:
    def test_returns_all_artifact_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            "teatree.core.harness_todos.read_harness_todos",
            lambda _sid: [],
        )
        monkeypatch.setattr(stop_snapshot, "write_resume_plan", lambda *a, **k: tmp_path / "resume-plan.md")
        (tmp_path / "resume-plan.md").write_text("x")
        main = tmp_path / "main"
        _init_main_clone(main)
        wt = tmp_path / "wt"
        _add_worktree(main, wt)
        (wt / "a.txt").write_text("dirty\n")

        result = stop_snapshot.prepare_stop("sess-1", str(wt), base=tmp_path / "resume")
        assert result.session_id == "sess-1"
        assert result.todos_path is not None
        assert result.resume_plan_path == tmp_path / "resume-plan.md"
        assert len(result.at_risk) == 1

    def test_resilient_when_one_phase_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def _boom(*_a: object, **_k: object) -> Path:
            raise RuntimeError

        monkeypatch.setattr(
            "teatree.core.harness_todos.read_harness_todos",
            lambda _sid: [],
        )
        monkeypatch.setattr(stop_snapshot, "write_resume_plan", _boom)
        # A raising phase must not abort the whole prepare_stop — the other
        # artifacts still land and it never raises.
        result = stop_snapshot.prepare_stop("sess-1", str(tmp_path), base=tmp_path / "resume")
        assert result.resume_plan_path is None
        assert result.todos_path is not None
