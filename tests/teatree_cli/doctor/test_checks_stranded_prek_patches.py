"""A pre-commit stash whose content reached no file is surfaced; every other one is not.

Functional: a real ``git`` checkout under ``tmp_path``, real patch files in a real
``PREK_HOME``, the real check, and the real stdout. Nothing is mocked — the two
things the check reads from its environment (``PREK_HOME`` and the cwd's git
root) are both set for real, because the whole question this check answers is
whether a file on disk agrees with a patch on disk.

The quiet cases carry the weight here. A check that reports every saved patch
would be right about the one incident and useless every day after it, since prek
writes a patch on every commit that has unstaged changes and deletes none of
them. So each silent case below is a control: a restore that SUCCEEDED, a patch
belonging to another checkout, and a patch older than the window must each
produce no output, and the teeth case proves the check can still fire.
"""

import io
import os
import shutil
import subprocess
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.cli.doctor.checks_stranded_prek_patches import check_stranded_prek_patches

_GIT_BIN = shutil.which("git") or "/usr/bin/git"


def _patch_text(path: str, added: str) -> str:
    """One saved stash: a single-line addition on top of a one-line file."""
    context = " base"
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,1 +1,2 @@\n{context}\n"
        f"+{added}\n"
    )


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git checkout as cwd, with a real empty PREK_HOME beside it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([_GIT_BIN, "init", "-q"], cwd=repo, check=True)
    (tmp_path / "prek" / "patches").mkdir(parents=True)
    monkeypatch.setenv("PREK_HOME", str(tmp_path / "prek"))
    monkeypatch.chdir(repo)
    return repo


def _save_patch(tmp_path: Path, name: str, *, path: str, added: str, age_days: float = 0.0) -> Path:
    patch = tmp_path / "prek" / "patches" / f"{name}.patch"
    patch.write_text(_patch_text(path, added))
    if age_days:
        old = patch.stat().st_mtime - age_days * 86400
        os.utime(patch, (old, old))
    return patch


class TestStrandedPrekPatchCheck:
    def test_no_patches_saved_says_nothing(self, checkout: Path, tmp_path: Path) -> None:
        ok, out = _echoes(check_stranded_prek_patches)

        assert ok is True
        assert out == ""

    def test_a_restored_stash_says_nothing(self, checkout: Path, tmp_path: Path) -> None:
        """The everyday case: the restore worked, so the addition is back in the file."""
        (checkout / "f.txt").write_text("base\nRESTORED-EDIT\n")
        _save_patch(tmp_path, "1000-1", path="f.txt", added="RESTORED-EDIT")

        ok, out = _echoes(check_stranded_prek_patches)

        assert ok is True
        assert out == ""

    def test_a_stash_whose_content_reached_no_file_is_reported(self, checkout: Path, tmp_path: Path) -> None:
        """The teeth: the restore failed, the tree reads clean, the patch still holds the work."""
        (checkout / "f.txt").write_text("base\n")
        patch = _save_patch(tmp_path, "1000-2", path="f.txt", added="PRECIOUS-UNSTAGED-WORK")

        ok, out = _echoes(check_stranded_prek_patches)

        assert ok is True
        assert "1 pre-commit stash(es)" in out
        assert str(patch) in out
        assert "f.txt" in out
        assert "git apply" in out

    def test_a_stash_older_than_the_window_says_nothing(self, checkout: Path, tmp_path: Path) -> None:
        """Past a day an abandoned edit and a lost one are the same bytes, so neither is claimed."""
        (checkout / "f.txt").write_text("base\n")
        _save_patch(tmp_path, "1000-3", path="f.txt", added="LONG-ABANDONED", age_days=3)

        ok, out = _echoes(check_stranded_prek_patches)

        assert ok is True
        assert out == ""

    def test_another_checkouts_patch_says_nothing(self, checkout: Path, tmp_path: Path) -> None:
        """The patch cache is one directory shared by every repo on the machine."""
        _save_patch(tmp_path, "1000-4", path="some/other/repo/thing.py", added="NOT-OURS")

        ok, out = _echoes(check_stranded_prek_patches)

        assert ok is True
        assert out == ""

    def test_a_partly_revised_draft_says_nothing(self, checkout: Path, tmp_path: Path) -> None:
        """A lane that revised its own draft keeps most of the block, so it never matches."""
        (checkout / "f.txt").write_text("base\nKEPT-LINE\nrewritten tail\n")
        patch = tmp_path / "prek" / "patches" / "1000-5.patch"
        patch.write_text(
            "diff --git a/f.txt b/f.txt\nindex 1111111..2222222 100644\n--- a/f.txt\n+++ b/f.txt\n"
            "@@ -1,1 +1,3 @@\n base\n+KEPT-LINE\n+draft tail\n"
        )

        ok, out = _echoes(check_stranded_prek_patches)

        assert ok is True
        assert out == ""

    def test_an_unreadable_cache_is_reported_unverified_not_silent(
        self, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe that could not run says so; it never reports a clean result it did not measure."""
        monkeypatch.setattr(
            "teatree.cli.doctor.checks_stranded_prek_patches._patch_dir",
            lambda: (_ for _ in ()).throw(OSError("cache unreadable")),
        )

        ok, out = _echoes(check_stranded_prek_patches)

        assert ok is True
        assert "UNVERIFIED" in out

    def test_outside_a_checkout_says_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no git root there is no tree to compare a patch against."""
        (tmp_path / "prek" / "patches").mkdir(parents=True)
        _save_patch(tmp_path, "1000-6", path="f.txt", added="ORPHAN")
        monkeypatch.setenv("PREK_HOME", str(tmp_path / "prek"))
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)

        ok, out = _echoes(check_stranded_prek_patches)

        assert ok is True
        assert out == ""
