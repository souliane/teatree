"""Pre-investigation stale-clone hard-fail gate (#948).

Before #948 a bug-investigation sub-agent could begin root-causing
against a repo clone many commits behind ``origin/<default>`` and form
an initially-wrong root-cause hypothesis before re-fetching. #940 covers
branch-currency *before cold review/ship*; this is the earlier point:
**before any bug investigation reads repo files**.

The gate fetches every in-scope repo, then asserts that
``origin/<default>`` is an ancestor of ``HEAD``. If a clone is behind it
hard-fails with an actionable "clone N commits behind — refusing to
investigate stale code; sync first" message — not a warning, a
deterministic refusal. Mirrors the ``schema_guard`` pattern (#869).
"""

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from teatree.cli import doctor as doctor_mod
from teatree.core.gates import clone_guard
from teatree.core.gates.clone_guard import (
    StaleCloneError,
    clones_behind_default,
    doctor_check_clone_currency,
    require_current_clones,
)


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
    def test_clones_behind_default_returns_empty_when_in_sync(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        assert clones_behind_default([("repo", clone)]) == []

    def test_require_current_clones_is_noop_when_in_sync(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        require_current_clones([("repo", clone)])  # must not raise

    def test_feature_branch_ahead_of_origin_is_current(self, tmp_path: Path) -> None:
        """A feature branch with commits on top of origin/main is NOT stale."""
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _git(clone, "checkout", "-b", "feature")
        (clone / "f.txt").write_text("local-work\n")
        _git(clone, "add", "f.txt")
        _git(clone, "commit", "-m", "local")
        require_current_clones([("repo", clone)])
        assert clones_behind_default([("repo", clone)]) == []


class TestClonesStale:
    def test_clones_behind_default_returns_count_when_stale(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        # Remote advances 3 commits; the clone is now 3 behind.
        _advance_remote(tmp_path, bare, n=3)

        staleness = clones_behind_default([("repo", clone)])

        assert len(staleness) == 1
        assert staleness[0].name == "repo"
        assert staleness[0].behind == 3
        assert staleness[0].default_branch == "main"

    def test_require_current_clones_raises_actionable_error(self, tmp_path: Path) -> None:
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare, n=2)

        with pytest.raises(StaleCloneError) as exc:
            require_current_clones([("repo", clone)])

        message = str(exc.value)
        assert "2 commit" in message
        assert "behind origin/main" in message
        assert "refusing to investigate stale code" in message
        assert "sync first" in message
        assert "git pull --ff-only" in message or "t3 update" in message

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

        with pytest.raises(StaleCloneError):
            require_current_clones([("repo", clone)])

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
        require_current_clones([("repo", seed)])  # must not raise

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
            # is what we pin here.
            try:
                ok = doctor_mod.check()
            except SystemExit as exc:  # typer may sys.exit on FAIL
                ok = exc.code == 0

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
        assert clones_behind_default([("repo", clone)]) == []

    def test_path_that_is_not_a_directory_is_skipped(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert clones_behind_default([("ghost", missing)]) == []
        require_current_clones([("ghost", missing)])  # must not raise

    def test_fetch_failure_is_treated_as_inconclusive_skip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failed fetch is an inconclusive skip, not a block."""
        bare = _make_remote(tmp_path)
        clone = _clone(tmp_path, bare)
        _advance_remote(tmp_path, bare, n=3)

        monkeypatch.setattr(clone_guard, "_fetch_origin", lambda repo: False)
        assert clone_guard.clones_behind_default([("repo", clone)]) == []
