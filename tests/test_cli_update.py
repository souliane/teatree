"""Integration tests for ``t3 update``.

Real ``git init`` repos with fake (local) remotes under ``tmp_path`` — no
mocking of ``git``, ``subprocess``, or the filesystem (Test-Writing Doctrine).

The only externals stubbed are the *reinstall* and *re-run setup* side
effects: those shell out to ``uv tool install`` / ``t3 setup`` against the
host machine and are out of scope for the git-sync behaviour under test.
The stubs are recording callables, not ``Mock()`` assertions on call_args.
"""

import subprocess
from pathlib import Path

import click
import pytest

from teatree.cli import update as update_mod
from teatree.cli.update import RepoUpdate, UpdateStatus, update_repo


def _git(cwd: Path, *args: str) -> str:
    # Trusted fixed argv (literal "git" + test-controlled flags). GIT_* env
    # is stripped session-wide by conftest so the tmp repo is isolated.
    result = subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_remote(tmp_path: Path, name: str = "remote") -> Path:
    """Create a bare remote with one commit on ``main``."""
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(seed, "config", "user.name", "Tester")
    (seed / "f.txt").write_text("v1\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-m", "initial")

    bare = tmp_path / f"{name}.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))
    return bare


def _clone(tmp_path: Path, bare: Path, name: str = "clone") -> Path:
    clone = tmp_path / name
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(clone, "config", "user.name", "Tester")
    return clone


def _advance_remote(tmp_path: Path, bare: Path) -> str:
    """Push a new commit to the bare remote; return its short sha."""
    work = tmp_path / "advance"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(work, "config", "user.name", "Tester")
    (work / "f.txt").write_text("v2\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "second")
    _git(work, "push", "origin", "main")
    return _git(work, "rev-parse", "--short", "HEAD")


class TestUpdateRepoCleanFastForward:
    def test_clean_on_default_branch_fast_forwards(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        old_sha = _git(clone, "rev-parse", "--short", "HEAD")
        new_sha = _advance_remote(tmp_path, bare)

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.UPDATED
        assert result.old_sha == old_sha
        assert result.new_sha == new_sha
        assert _git(clone, "rev-parse", "--short", "HEAD") == new_sha


class TestUpdateRepoUpToDate:
    def test_already_up_to_date_is_not_an_error(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.UP_TO_DATE
        assert result.is_error is False


class TestUpdateRepoSkips:
    def test_dirty_checkout_skips_without_clobbering(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare)
        (clone / "f.txt").write_text("local uncommitted work\n")

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.SKIPPED
        assert "dirty" in result.reason.lower()
        assert result.is_error is False
        # Never clobbered — the local edit survives.
        assert (clone / "f.txt").read_text() == "local uncommitted work\n"

    def test_feature_branch_checkout_skips(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature/wip")

        result = update_repo("clone", clone)

        assert result.status is UpdateStatus.SKIPPED
        assert "branch" in result.reason.lower()
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "feature/wip"

    def test_no_upstream_skips(self, tmp_path: Path) -> None:
        seed = tmp_path / "no-remote"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
        _git(seed, "config", "user.name", "Tester")
        (seed / "f.txt").write_text("v1\n")
        _git(seed, "add", "f.txt")
        _git(seed, "commit", "-m", "initial")

        result = update_repo("no-remote", seed)

        assert result.status is UpdateStatus.SKIPPED
        assert result.is_error is False


class TestRepoUpdateSummary:
    def test_summary_line_shapes(self) -> None:
        updated = RepoUpdate("core", UpdateStatus.UPDATED, old_sha="aaaaaaa", new_sha="bbbbbbb")
        up = RepoUpdate("ovl", UpdateStatus.UP_TO_DATE)
        skipped = RepoUpdate("ovl2", UpdateStatus.SKIPPED, reason="dirty working tree")

        assert "aaaaaaa" in updated.summary_line
        assert "bbbbbbb" in updated.summary_line
        assert "up-to-date" in up.summary_line
        assert "skipped" in skipped.summary_line
        assert "dirty working tree" in skipped.summary_line
        assert updated.is_error is False
        assert RepoUpdate("x", UpdateStatus.FAILED, reason="boom").is_error is True


class TestUpdateCommandExitCode:
    """The typer command exits non-zero only on a hard failure, not a skip."""

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, results: list[RepoUpdate]) -> int:
        monkeypatch.setattr(update_mod, "_collect_repos", lambda: [("core", tmp_path)])
        monkeypatch.setattr(update_mod, "update_repo", lambda name, path: results.pop(0))
        monkeypatch.setattr(update_mod, "_reinstall_and_resetup", lambda repos: None)

        try:
            update_mod._run_update()
        except (SystemExit, click.exceptions.Exit) as exc:
            code = exc.code if isinstance(exc, SystemExit) else exc.exit_code
            return int(code or 0)
        return 0

    def test_skip_only_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        code = self._run(
            tmp_path,
            monkeypatch,
            [RepoUpdate("core", UpdateStatus.SKIPPED, reason="dirty working tree")],
        )
        assert code == 0

    def test_hard_failure_exits_nonzero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        code = self._run(
            tmp_path,
            monkeypatch,
            [RepoUpdate("core", UpdateStatus.FAILED, reason="git fetch failed")],
        )
        assert code != 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
