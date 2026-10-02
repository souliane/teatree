"""Untracked, non-ignored content in a checkout is surfaced; a clean or ignored one is not.

Functional: a real git checkout under ``tmp_path``, real files, the real check, and
the real stdout. Nothing is mocked — the whole question is whether ``git status``
agrees with what is on disk.

Every case here runs from a cwd that is NOT the checkout, because that is the only venue
this check ever runs in: ``t3`` executes in a container whose WORKDIR is no repository, so
a fixture that chdirs into the repo manufactures the one condition production never has and
scores a check that reports nothing as passing (#4727).
"""

import io
import shutil
import subprocess
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.cli.doctor.checks_checkout_debris import check_checkout_untracked_debris

_GIT_BIN = shutil.which("git") or "/usr/bin/git"


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git checkout with an initial commit, declared from a cwd that is not it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([_GIT_BIN, "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([_GIT_BIN, "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run([_GIT_BIN, "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("hello\n")
    subprocess.run([_GIT_BIN, "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run([_GIT_BIN, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    elsewhere = tmp_path / "plain"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(repo))
    return repo


class TestCheckoutUntrackedDebrisCheck:
    def test_a_clean_checkout_says_nothing(self, checkout: Path) -> None:
        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert out == ""

    def test_an_ignored_path_says_nothing(self, checkout: Path) -> None:
        (checkout / ".gitignore").write_text("scratch/\n")
        subprocess.run([_GIT_BIN, "add", ".gitignore"], cwd=checkout, check=True)
        subprocess.run([_GIT_BIN, "commit", "-q", "-m", "ignore scratch"], cwd=checkout, check=True)
        (checkout / "scratch").mkdir()
        (checkout / "scratch" / "junk.txt").write_text("x\n")

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert out == ""

    def test_an_untracked_directory_is_reported_once_not_per_file(self, checkout: Path) -> None:
        held = checkout / ".review-scratch" / "held"
        held.mkdir(parents=True)
        (held / "a.py").write_text("x = 1\n")
        (held / "b.py").write_text("y = 2\n")

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert "1 untracked path(s)" in out
        assert ".review-scratch/" in out
        assert "ty-check" in out

    def test_an_untracked_file_is_reported(self, checkout: Path) -> None:
        (checkout / "stray.md").write_text("draft\n")

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert "1 untracked path(s)" in out
        assert "stray.md" in out

    def test_many_untracked_paths_are_truncated_with_an_honest_count(self, checkout: Path) -> None:
        for i in range(15):
            (checkout / f"stray-{i:02d}.txt").write_text("x\n")

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert "15 untracked path(s)" in out
        assert "… and 5 more" in out

    def test_an_unreadable_repo_root_is_reported_unverified_not_silent(
        self, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "teatree.cli.doctor.checkout_root.doctor_checkout_root",
            lambda: (_ for _ in ()).throw(OSError("git unreadable")),
        )

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert "crashed" in out

    def test_no_checkout_anywhere_says_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no checkout named by either bearing, there is nothing to read and nothing to say."""
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.setattr("teatree.cli.doctor.checkout_root.installed_clone_root", lambda: plain)

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert out == ""

    def test_fires_from_the_installed_clone_when_nothing_is_declared(
        self, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deployed venue: no declaration at all, so the clone is the only bearing left."""
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.setattr("teatree.cli.doctor.checkout_root.installed_clone_root", lambda: checkout)
        (checkout / "stray.md").write_text("draft\n")

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert "stray.md" in out

    def test_a_path_with_a_space_is_named_raw_not_c_quoted(self, checkout: Path) -> None:
        """Text porcelain C-quotes such a path, yielding a string that names no file."""
        (checkout / "has space.txt").write_text("x\n")

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert "has space.txt" in out
        assert '"has space.txt"' not in out

    def test_an_inconclusive_status_is_reported_unverified_not_clean(self, checkout: Path) -> None:
        """A `git status` that failed must never read as a clean tree.

        A corrupt index is the shape that isolates this: ``rev-parse`` still names the
        checkout, so the root resolves, and only the status read fails.
        """
        (checkout / "stray.md").write_text("draft\n")
        (checkout / ".git" / "index").write_bytes(b"GARBAGE-NOT-AN-INDEX")

        ok, out = _echoes(check_checkout_untracked_debris)

        assert ok is True
        assert "UNVERIFIED" in out
