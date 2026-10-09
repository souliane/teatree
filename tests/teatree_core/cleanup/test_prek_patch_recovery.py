"""prek's leftover patches: which ones hold work that is nowhere else (#144).

prek clears unstaged changes before a hook run and restores them at the end. The
restore is skipped on SIGTERM and SIGKILL, so a killed run leaves the working tree
holding only the STAGED version, with the patch as the only surviving copy. It
also keeps a patch after a SUCCESSFUL restore, so the file's existence proves
nothing — only the TREE can say which is which. These drive real ``git apply``
over real patches so that distinction is measured, never assumed.
"""

import subprocess
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.cleanup.prek_patch_recovery import (
    PatchState,
    classify_patch,
    classify_patches,
    render_report,
    restore_patch,
)
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class _PrekPatchScene(TestCase):
    """A repo whose tracked file has a committed baseline, plus a patch dir."""

    @pytest.fixture(autouse=True)
    def _scene(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo)
        _run_git("config", "user.email", "t@t", cwd=self.repo)
        _run_git("config", "user.name", "t", cwd=self.repo)
        self.tracked = self.repo / "work.txt"
        self.tracked.write_text("line1\n", encoding="utf-8")
        _run_git("add", "work.txt", cwd=self.repo)
        _run_git("commit", "-q", "-m", "baseline", cwd=self.repo)
        self.patch_dir = tmp_path / "patches"
        self.patch_dir.mkdir()

    def _patch_adding(self, name: str, line: str, *, path: str = "work.txt") -> Path:
        """A prek-shaped patch that appends ``line`` — captured VERBATIM from a real diff.

        Verbatim because a stripped patch is one ``git apply`` rejects as corrupt
        (#4435), which would make every classification below pass for the wrong reason.
        """
        target = self.repo / path
        if not target.exists():
            target.write_text("line1\n", encoding="utf-8")
            _run_git("add", path, cwd=self.repo)
            _run_git("commit", "-q", "-m", f"add {path}", cwd=self.repo)
        target.write_text(f"line1\n{line}\n", encoding="utf-8")
        diff = subprocess.run(
            [_GIT, "-C", str(self.repo), "diff", "--binary"],
            check=True,
            capture_output=True,
            env=_clean_env(),
        ).stdout
        _run_git("checkout", "--", path, cwd=self.repo)
        patch = self.patch_dir / name
        patch.write_bytes(diff)
        return patch


class TestClassification(_PrekPatchScene):
    def test_patch_whose_content_is_absent_from_the_tree_is_restorable(self) -> None:
        patch = self._patch_adding("1788454540977-18066.patch", "PRECIOUS")

        assert classify_patch(self.repo, patch).state is PatchState.RESTORABLE

    def test_patch_whose_content_is_already_in_the_tree_is_litter(self) -> None:
        patch = self._patch_adding("1788454540978-18067.patch", "PRECIOUS")
        self.tracked.write_text("line1\nPRECIOUS\n", encoding="utf-8")

        assert classify_patch(self.repo, patch).state is PatchState.ALREADY_PRESENT

    def test_patch_for_another_repo_is_unknown_never_restorable(self) -> None:
        foreign = self.patch_dir / "1788454540979-18068.patch"
        foreign.write_text(
            "diff --git a/absent.txt b/absent.txt\n"
            "index 1111111..2222222 100644\n"
            "--- a/absent.txt\n"
            "+++ b/absent.txt\n"
            "@@ -1 +1,2 @@\n"
            " nothing\n"
            "+here\n",
            encoding="utf-8",
        )

        assert classify_patch(self.repo, foreign).state is PatchState.UNKNOWN

    def test_an_empty_patch_holds_nothing_and_is_never_offered_as_loss(self) -> None:
        empty = self.patch_dir / "1788454540980-18069.patch"
        empty.write_text("", encoding="utf-8")

        assert classify_patch(self.repo, empty).state is not PatchState.RESTORABLE

    def test_classify_patches_reads_the_whole_dir_newest_first(self) -> None:
        older = self._patch_adding("1788454540900-1.patch", "OLDER")
        newer = self._patch_adding("1788454540999-2.patch", "NEWER")

        assert [p.path for p in classify_patches(self.repo, self.patch_dir)] == [newer, older]


class TestRestore(_PrekPatchScene):
    def test_restore_puts_the_killed_runs_work_back_in_the_tree(self) -> None:
        patch = self._patch_adding("1788454541000-19000.patch", "PRECIOUS")

        outcome = restore_patch(self.repo, patch)

        assert outcome.ok
        assert "PRECIOUS" in self.tracked.read_text(encoding="utf-8")

    def test_dry_run_reports_without_writing(self) -> None:
        patch = self._patch_adding("1788454541001-19001.patch", "PRECIOUS")

        outcome = restore_patch(self.repo, patch, dry_run=True)

        assert outcome.ok
        assert "PRECIOUS" not in self.tracked.read_text(encoding="utf-8")

    def test_restore_refuses_a_patch_that_does_not_apply_and_writes_nothing(self) -> None:
        foreign = self.patch_dir / "1788454541002-19002.patch"
        foreign.write_text(
            "diff --git a/absent.txt b/absent.txt\n"
            "index 1111111..2222222 100644\n"
            "--- a/absent.txt\n"
            "+++ b/absent.txt\n"
            "@@ -1 +1,2 @@\n"
            " nothing\n"
            "+here\n",
            encoding="utf-8",
        )

        outcome = restore_patch(self.repo, foreign)

        assert not outcome.ok
        assert not (self.repo / "absent.txt").exists()

    def test_nothing_here_deletes_the_patch_it_just_restored(self) -> None:
        patch = self._patch_adding("1788454541003-19003.patch", "PRECIOUS")

        restore_patch(self.repo, patch)

        assert patch.exists()


class TestReport(_PrekPatchScene):
    def test_report_leads_with_the_restorable_count_and_names_the_recovery_command(self) -> None:
        # Two INDEPENDENT files, so one patch's verdict cannot be an artefact of the other's.
        self._patch_adding("1788454542000-20000.patch", "PRECIOUS")
        litter = self._patch_adding("1788454542001-20001.patch", "ALREADY", path="second.txt")
        (self.repo / "second.txt").write_text("line1\nALREADY\n", encoding="utf-8")

        report = render_report(self.repo, self.patch_dir)

        assert "1 restorable of 2" in report
        assert report.index("1788454542000-20000.patch") < report.index(litter.name)
        assert "--restore 1788454542000-20000.patch" in report

    def test_an_empty_patch_dir_says_so_rather_than_rendering_a_bare_header(self) -> None:
        assert "none" in render_report(self.repo, self.patch_dir)


class TestRenderedVerdict(_PrekPatchScene):
    def test_a_dry_run_never_reads_as_a_restore_that_happened(self) -> None:
        patch = self._patch_adding("1788454543000-21000.patch", "PRECIOUS")

        assert restore_patch(self.repo, patch, dry_run=True).render().startswith("would restore")
        assert restore_patch(self.repo, patch).render().startswith("restored")
