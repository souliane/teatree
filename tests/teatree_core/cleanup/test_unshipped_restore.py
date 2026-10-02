"""Reading a salvage bundle back — the half that keeps the capture honest (#4435).

Bundles that ``git apply`` rejected as ``corrupt patch`` accumulated for as long
as nothing read one back: the first person to try was an operator recovering real
work by hand. These drive the real ``git apply`` over a real capture, so a capture
that regresses to a stripped patch fails HERE rather than mid-recovery.
"""

from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.cleanup.unshipped_restore import ORDERED_PARTS, resolve_prefix, restore_bundle
from teatree.core.cleanup.unshipped_work import (
    COMMITS_SUFFIX,
    FILES_SUFFIX,
    UNCOMMITTED_SUFFIX,
    UNREADABLE_SUFFIX,
    bundle_path,
    capture_unshipped_work,
)
from teatree.core.models import UnshippedWorkRecord
from tests.teatree_core.cleanup._shared import _run_git


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class _CapturedBundle(TestCase):
    """A checkout holding every recoverable state, captured, plus a pristine target."""

    @pytest.fixture(autouse=True)
    def _tmp_capture(self, tmp_path: Path) -> None:
        self.artifacts = tmp_path / "artifacts"
        self.origin = tmp_path / "origin.git"
        self.origin.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        self.clone = tmp_path / "clone"
        self.clone.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.clone)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)
        _run_git("remote", "add", "origin", str(self.origin), cwd=self.clone)
        (self.clone / "tracked.py").write_text("value = 1\n\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "initial", cwd=self.clone)
        _run_git("push", "-q", "-u", "origin", "main", cwd=self.clone)
        self.checkout = tmp_path / "agent-deadbeef"
        _run_git("worktree", "add", "-q", "-b", "feat", str(self.checkout), cwd=self.clone)
        self.into = tmp_path / "restore"
        _run_git("worktree", "add", "-q", "--detach", str(self.into), "origin/main", cwd=self.clone)

    def _dirty_and_capture(self) -> UnshippedWorkRecord:
        """An unpushed commit, a staged edit on top of it, and an untracked file."""
        (self.checkout / "committed.py").write_text("shipped = False\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "local only", cwd=self.checkout)
        (self.checkout / "tracked.py").write_text("value = 2\n\n", encoding="utf-8")
        _run_git("add", "tracked.py", cwd=self.checkout)
        (self.checkout / "scratch.md").write_text("notes\n", encoding="utf-8")
        record = capture_unshipped_work(self.checkout, branch="feat", overlay="test", artifact_root=self.artifacts)
        assert record is not None
        return record


class TestTheBundleRestoresWhatWasCaptured(_CapturedBundle):
    def test_every_part_applies_and_the_files_come_back_byte_exact(self) -> None:
        record = self._dirty_and_capture()

        outcome = restore_bundle(record.checkout_path, self.into)

        assert outcome.ok, outcome.render()
        assert set(outcome.parts) == set(ORDERED_PARTS)
        for name in ("committed.py", "tracked.py", "scratch.md"):
            assert (self.into / name).read_bytes() == (self.checkout / name).read_bytes(), name

    def test_a_dry_run_reports_each_part_applies_and_writes_nothing(self) -> None:
        record = self._dirty_and_capture()

        outcome = restore_bundle(record.checkout_path, self.into, dry_run=True)

        assert outcome.ok, outcome.render()
        assert all("nothing written" in part for part in outcome.parts.values())
        assert not (self.into / "scratch.md").exists()
        assert _read(self.into / "tracked.py") == "value = 1\n\n"

    def test_an_empty_part_is_skipped_rather_than_applied_as_a_patch(self) -> None:
        (self.checkout / "tracked.py").write_text("value = 9\n\n", encoding="utf-8")
        record = capture_unshipped_work(self.checkout, branch="feat", artifact_root=self.artifacts)
        assert record is not None
        assert not bundle_path(record.artifact_prefix, COMMITS_SUFFIX).stat().st_size

        outcome = restore_bundle(record.artifact_prefix, self.into)

        assert outcome.ok, outcome.render()
        assert list(outcome.parts) == [UNCOMMITTED_SUFFIX]

    def test_the_prefix_itself_is_accepted_as_the_reference(self) -> None:
        record = self._dirty_and_capture()

        outcome = restore_bundle(record.artifact_prefix, self.into, dry_run=True)

        assert outcome.ok, outcome.render()
        assert outcome.prefix == record.artifact_prefix

    def test_the_restore_stays_hermetic_under_an_inherited_git_env(self) -> None:
        """A pre-commit hook exports GIT_DIR/GIT_WORK_TREE, which would hijack the apply."""
        record = self._dirty_and_capture()

        with pytest.MonkeyPatch.context() as env:
            env.setenv("GIT_DIR", str(self.clone / ".git"))
            env.setenv("GIT_WORK_TREE", str(self.clone))
            outcome = restore_bundle(record.checkout_path, self.into)

        assert outcome.ok, outcome.render()
        assert _read(self.into / "scratch.md") == "notes\n"
        assert not (self.clone / "scratch.md").exists(), "the ambient GIT_DIR must not receive the restore"


class TestARestoreThatCannotProceedSaysWhy(_CapturedBundle):
    def test_a_target_that_is_not_a_checkout_is_refused_before_any_apply(self) -> None:
        record = self._dirty_and_capture()
        plain = self.into.parent / "not-a-checkout"
        plain.mkdir()

        outcome = restore_bundle(record.checkout_path, plain)

        assert not outcome.ok
        assert "is not a git checkout" in outcome.errors[0]
        assert not outcome.parts

    def test_an_absent_bundle_is_named_rather_than_reported_as_a_clean_restore(self) -> None:
        outcome = restore_bundle(str(self.artifacts / "never-captured"), self.into)

        assert not outcome.ok
        assert "no salvage bundle at" in outcome.errors[0]

    def test_an_unreadable_capture_reports_the_cause_it_recorded(self) -> None:
        prefix = self.artifacts / "unreadable-checkout"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        bundle_path(str(prefix), UNREADABLE_SUFFIX).write_text("no resolvable HEAD\n", encoding="utf-8")

        outcome = restore_bundle(str(prefix), self.into)

        assert not outcome.ok
        assert "captured no content" in outcome.errors[0]
        assert "no resolvable HEAD" in outcome.errors[0]

    def test_a_bundle_holding_only_empty_patches_refuses_rather_than_restoring_nothing(self) -> None:
        """The capture degrades to the tracked-only delta, so an untracked-only checkout leaves these."""
        prefix = self.artifacts / "content-free"
        prefix.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ORDERED_PARTS:
            bundle_path(str(prefix), suffix).write_text("", encoding="utf-8")
        bundle_path(str(prefix), FILES_SUFFIX).write_text("scratch.md\n", encoding="utf-8")

        outcome = restore_bundle(str(prefix), self.into)

        assert not outcome.ok
        assert "holds no patch content" in outcome.errors[0]
        assert FILES_SUFFIX in outcome.errors[0], "the operator needs pointing at what WAS there"

    def test_a_part_that_does_not_apply_fails_loudly_and_leaves_the_target_untouched(self) -> None:
        record = self._dirty_and_capture()
        (self.into / "tracked.py").write_text("someone else got here first\n", encoding="utf-8")

        outcome = restore_bundle(record.artifact_prefix, self.into)

        assert not outcome.ok
        assert any(UNCOMMITTED_SUFFIX in error for error in outcome.errors), outcome.render()
        assert _read(self.into / "tracked.py") == "someone else got here first\n"

    def test_the_render_names_the_target_and_every_error(self) -> None:
        outcome = restore_bundle(str(self.artifacts / "never-captured"), self.into)

        rendered = outcome.render()

        assert str(self.into) in rendered
        assert "ERROR:" in rendered


class TestResolvePrefix(_CapturedBundle):
    def test_a_recorded_checkout_path_resolves_to_its_artifact_prefix(self) -> None:
        record = self._dirty_and_capture()

        assert resolve_prefix(record.checkout_path) == record.artifact_prefix

    def test_an_unrecorded_reference_is_taken_as_the_prefix_itself(self) -> None:
        assert resolve_prefix("/tmp/never-recorded") == "/tmp/never-recorded"


class TestTheWorkspaceRestoreCommand(_CapturedBundle):
    """`t3 <overlay> workspace restore` — the surface an operator in a hurry actually reaches."""

    def test_it_applies_the_bundle_and_reports_each_part(self) -> None:
        record = self._dirty_and_capture()

        rendered = call_command("workspace", "restore", record.checkout_path, "--into", str(self.into))

        assert "applied" in rendered
        assert _read(self.into / "scratch.md") == "notes\n"

    def test_it_refuses_without_into_rather_than_inferring_a_target(self) -> None:
        record = self._dirty_and_capture()

        with pytest.raises(SystemExit) as exit_info:
            call_command("workspace", "restore", record.checkout_path)

        assert exit_info.value.code == 1
        assert not (self.into / "scratch.md").exists()

    def test_a_failed_restore_exits_non_zero_so_a_caller_can_branch(self) -> None:
        with pytest.raises(SystemExit) as exit_info:
            call_command("workspace", "restore", str(self.artifacts / "never-captured"), "--into", str(self.into))

        assert exit_info.value.code == 1
