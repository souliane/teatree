"""Session-start clone-currency check (#948).

``t3 doctor check`` fetches every registered clone and FAILs on each one
whose ``origin/<default>`` is not an ancestor of ``HEAD``. A clone whose
currency cannot be read is a WARN naming why, never a silent "current".
Real ``git`` under ``tmp_path`` throughout.
"""

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.cli import doctor as doctor_mod
from teatree.core.gates import clone_guard
from teatree.core.gates.clone_guard import clone_currency, doctor_check_clone_currency


def _git(cwd: Path, *args: str) -> str:
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


def _advance_remote(tmp_path: Path, bare: Path, n: int = 1) -> None:
    """Push *n* new commits to the bare remote's default branch."""
    work = tmp_path / f"advance-{bare.name}"
    if work.exists():
        # Reuse existing advance clone if called repeatedly
        for i in range(n):
            (work / "f.txt").write_text(f"more-{i}\n")
            _git(work, "add", "f.txt")
            _git(work, "commit", "-m", f"advance-{i}")
        _git(work, "push", "origin", "main")
        return
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(work, "config", "user.name", "Tester")
    for i in range(n):
        (work / "f.txt").write_text(f"more-{i}\n")
        _git(work, "add", "f.txt")
        _git(work, "commit", "-m", f"advance-{i}")
    _git(work, "push", "origin", "main")


class TestClonesCurrent:
    def test_a_clone_in_sync_is_neither_stale_nor_unverified(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        survey = clone_currency([("repo", clone)])
        assert survey.stale == []
        assert survey.unverified == []

    def test_feature_branch_ahead_of_origin_is_current(self, tmp_path: Path) -> None:
        """A feature branch with commits on top of origin/main is NOT stale."""
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature")
        (clone / "f.txt").write_text("local-work\n")
        _git(clone, "add", "f.txt")
        _git(clone, "commit", "-m", "local")
        assert clone_currency([("repo", clone)]).stale == []


class TestClonesStale:
    def test_a_clone_behind_its_remote_is_stale_by_that_count(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        # Remote advances 3 commits; the clone is now 3 behind.
        _advance_remote(tmp_path, bare, n=3)

        staleness = clone_currency([("repo", clone)]).stale

        assert len(staleness) == 1
        assert staleness[0].name == "repo"
        assert staleness[0].behind == 3
        assert staleness[0].default_branch == "main"

    def test_feature_branch_diverged_from_advanced_origin_is_stale(self, tmp_path: Path) -> None:
        """origin/main advanced past the feature branch point."""
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature")
        (clone / "f.txt").write_text("local-work\n")
        _git(clone, "add", "f.txt")
        _git(clone, "commit", "-m", "local")
        # Now advance origin/main past the branch point
        _advance_remote(tmp_path, bare, n=1)

        assert [finding.behind for finding in clone_currency([("repo", clone)]).stale] == [1]

    def test_doctor_surface_fails_and_names_repos(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare, n=4)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = doctor_check_clone_currency([("repo", clone)])

        assert ok is False
        out = buffer.getvalue()
        assert "FAIL" in out
        assert "repo" in out
        assert "behind origin/main" in out
        assert "4 commit(s)" in out
        assert "t3 update" in out


class TestEdgeCases:
    def test_skips_repo_without_origin(self, tmp_path: Path) -> None:
        """A clone without origin/HEAD is skipped silently."""
        seed = tmp_path / "no-origin"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
        _git(seed, "config", "user.name", "Tester")
        (seed / "f.txt").write_text("v1\n")
        _git(seed, "add", "f.txt")
        _git(seed, "commit", "-m", "initial")
        # No origin remote at all.
        assert clone_currency([("repo", seed)]) == clone_currency([])

    def test_doctor_check_passes_when_all_current(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        assert doctor_check_clone_currency([("repo", clone)]) is True


class TestDoctorAggregation:
    """Doctor check aggregates the clone-currency surface."""

    def test_doctor_check_calls_clone_currency_surface(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare, n=2)

        # Steer `doctor_check_clone_currency()` (called with no args by the
        # aggregator) at this stale clone via the `_collect_repos` indirection
        # the function uses when `repos is None`. No mock — real `git` under
        # `tmp_path` runs the full fetch + ancestor check.
        monkeypatch.setattr(
            "teatree.cli.update._collect_repos",
            lambda: [("repo", clone)],
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            # `_check_singletons` / `_ensure_plugin_registered` are real-env
            # side-effects we tolerate — only the clone-currency aggregation
            # is what we pin here. `run_doctor_checks` is the bool core the
            # `check` command wraps into an exit code.
            ok = doctor_mod.run_doctor_checks()

        out = buffer.getvalue()
        assert ok is False
        assert "behind origin/main" in out
        assert "t3 update" in out


class TestDefensiveBranches:
    """Defensive skip paths are silent (no false-positive blocks)."""

    def test_clone_with_origin_remote_but_no_origin_head_is_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unresolvable origin/HEAD is silently skipped."""
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        monkeypatch.setattr(clone_guard, "_default_branch", lambda repo: None)
        assert clone_currency([("repo", clone)]) == clone_currency([])

    def test_path_that_is_not_a_directory_is_skipped(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert clone_currency([("ghost", missing)]) == clone_currency([])


class TestAnUnreadableCloneIsReportedNotPassed:
    """A clone whose currency cannot be read is UNKNOWN: a WARN line, never a silent "current"."""

    def _doctor(self, clone: Path) -> tuple[bool, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ok = doctor_check_clone_currency([("repo", clone)])
        return ok, buffer.getvalue()

    def test_a_failed_fetch_warns_without_gating(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

        ok, out = self._doctor(clone)

        assert ok is True
        assert "WARN" in out
        assert "repo" in out
        assert "currency not verified" in out
        assert "git fetch origin failed" in out
        assert [clone.name for clone in clone_currency([("repo", clone)]).unverified] == ["repo"]

    def test_an_origin_head_naming_a_pruned_branch_warns_without_gating(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "config", "fetch.prune", "true")
        _git(bare, "branch", "-m", "main", "trunk")

        ok, out = self._doctor(clone)

        assert ok is True
        assert "WARN" in out
        assert "currency not verified" in out
        assert "origin/main does not resolve" in out
        assert clone_currency([("repo", clone)]).stale == []
