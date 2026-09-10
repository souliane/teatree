"""The doctor's checkout root is resolved from a declaration or the clone, never the cwd.

Functional: real ``git`` checkouts under ``tmp_path``, the real resolver, and a cwd that is
deliberately NOT a checkout in every case — the container venue this resolver exists for.
``teatree.__file__`` is read for real in the live-pin test, so nothing there is mocked.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.cli.doctor.checkout_root import doctor_checkout_root, installed_clone_root

_GIT_BIN = shutil.which("git") or "/usr/bin/git"


def _init(repo: Path) -> Path:
    repo.mkdir(parents=True)
    subprocess.run([_GIT_BIN, "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([_GIT_BIN, "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run([_GIT_BIN, "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("hello\n")
    subprocess.run([_GIT_BIN, "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run([_GIT_BIN, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cwd that is not a checkout — what the container's image WORKDIR always is."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    return plain


class TestDoctorCheckoutRoot:
    def test_a_declared_cwd_resolves_to_its_checkout(
        self, tmp_path: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init(tmp_path / "repo")
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(repo))

        assert doctor_checkout_root() == repo.resolve()

    def test_a_declared_subdirectory_resolves_to_the_checkout_root(
        self, tmp_path: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init(tmp_path / "repo")
        nested = repo / "src" / "deep"
        nested.mkdir(parents=True)
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(nested))

        assert doctor_checkout_root() == repo.resolve()

    def test_a_declared_worktree_resolves_to_itself_not_its_clone(
        self, tmp_path: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The operator standing in a worktree is asking about THAT checkout."""
        repo = _init(tmp_path / "repo")
        linked = tmp_path / "linked"
        subprocess.run([_GIT_BIN, "worktree", "add", "-q", "-b", "side", str(linked)], cwd=repo, check=True)
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(linked))

        assert doctor_checkout_root() == linked.resolve()

    def test_a_declared_cwd_outside_any_checkout_falls_back_to_the_installed_clone(
        self, tmp_path: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init(tmp_path / "repo")
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.setenv("TEATREE_INVOCATION_CWD", str(outside))
        monkeypatch.setattr("teatree.cli.doctor.checkout_root.installed_clone_root", lambda: repo)

        assert doctor_checkout_root() == repo.resolve()

    def test_nothing_declared_falls_back_to_the_installed_clone(
        self, tmp_path: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init(tmp_path / "repo")
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.setattr("teatree.cli.doctor.checkout_root.installed_clone_root", lambda: repo)

        assert doctor_checkout_root() == repo.resolve()

    def test_no_checkout_anywhere_resolves_to_nothing(
        self, tmp_path: Path, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        monkeypatch.setattr("teatree.cli.doctor.checkout_root.installed_clone_root", lambda: outside)

        assert doctor_checkout_root() is None

    def test_the_live_process_resolves_its_own_clone_from_a_foreign_cwd(
        self, elsewhere: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The venue pin: nothing mocked, cwd not a checkout, and it still finds the clone.

        Skipped only where teatree is installed non-editable, which has no clone to find.
        """
        monkeypatch.delenv("TEATREE_INVOCATION_CWD", raising=False)
        clone = installed_clone_root()
        if not (clone / ".git").exists():
            pytest.skip("teatree is not installed from a git checkout in this environment")

        assert doctor_checkout_root() == clone.resolve()
