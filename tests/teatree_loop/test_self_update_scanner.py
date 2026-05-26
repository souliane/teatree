"""Tests for :class:`SelfUpdateScanner` — auto-pull teatree+overlays per tick (#1249).

The scanner runs every loop tick, walks the configured list of editable
clones (teatree core + every registered overlay), and fast-forwards each
clone to its ``origin/<default-branch>`` when the cadence has elapsed
AND the local working tree is clean AND the checkout is on the default
branch. It never rebases, never clobbers uncommitted local changes, and
never disturbs a feature-branch checkout — those are skips with a
descriptive reason, not errors.

The scanner persists a per-repo ``SelfUpdateMarker`` row that records
the last-pull-at timestamp + outcome so the cadence is honoured across
tick boundaries (a 1-minute tick cadence does not become a 1-minute git
fetch cadence — that's the whole point of the cadence-elapsed gate).

These tests use real ``git`` against ``tmp_path`` clones rather than
mocks: the scanner's contract is the git invocations it issues and the
SHAs it observes afterwards. A bare repo seeded with two commits is
cloned twice (with no shared object store via ``--no-local``) so the
"clone trails by one commit" case is naturally reproducible.
"""

import datetime as _dt
import os
import subprocess
from pathlib import Path

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.self_update_marker import SelfUpdateMarker
from teatree.loop.scanners.self_update import SelfUpdateScanner

pytestmark = pytest.mark.django_db


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Thin ``subprocess.run`` wrapper — capture text output and require exit 0."""
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_env() -> dict[str, str]:
    """Deterministic git env that lets ``commit`` succeed in a CI sandbox."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    return env


def _seed_origin_with_two_commits(origin_dir: Path) -> None:
    """Create a bare repo with two commits on ``main`` for ff-pull testing."""
    env = _git_env()
    seed = origin_dir.parent / f"_seed_{origin_dir.name}"
    seed.mkdir()
    _run("git", "init", "--initial-branch=main", str(seed), cwd=origin_dir.parent, env=env)
    (seed / "a.txt").write_text("a")
    _run("git", "add", "a.txt", cwd=seed, env=env)
    _run("git", "commit", "-m", "first", cwd=seed, env=env)
    (seed / "b.txt").write_text("b")
    _run("git", "add", "b.txt", cwd=seed, env=env)
    _run("git", "commit", "-m", "second", cwd=seed, env=env)
    _run("git", "init", "--bare", "--initial-branch=main", str(origin_dir), cwd=origin_dir.parent, env=env)
    _run("git", "remote", "add", "origin", str(origin_dir), cwd=seed, env=env)
    _run("git", "push", "-u", "origin", "main", cwd=seed, env=env)


def _clone_trailing_by_one_commit(*, origin: Path, clone: Path) -> str:
    """Clone *origin* to *clone*, reset HEAD back one commit so a ff-pull is needed.

    Returns the SHA the clone is checked out on (one behind ``origin/main``).
    """
    env = _git_env()
    _run("git", "clone", "--no-local", str(origin), str(clone), cwd=clone.parent, env=env)
    # Set origin/HEAD so _default_branch resolves to "main".
    _run("git", "remote", "set-head", "origin", "main", cwd=clone, env=env)
    _run("git", "reset", "--hard", "HEAD~1", cwd=clone, env=env)
    return _run("git", "rev-parse", "HEAD", cwd=clone, env=env).stdout.strip()


def _clone_up_to_date(*, origin: Path, clone: Path) -> str:
    """Clone *origin* to *clone* with no trailing commit — already up to date."""
    env = _git_env()
    _run("git", "clone", "--no-local", str(origin), str(clone), cwd=clone.parent, env=env)
    _run("git", "remote", "set-head", "origin", "main", cwd=clone, env=env)
    return _run("git", "rev-parse", "HEAD", cwd=clone, env=env).stdout.strip()


def _checkout_feature_branch(clone: Path) -> None:
    env = _git_env()
    _run("git", "checkout", "-b", "feat-x", cwd=clone, env=env)


def _make_tracked_dirty(clone: Path) -> None:
    """Modify a tracked file so the working tree is dirty."""
    tracked = clone / "a.txt"
    tracked.write_text("a-modified")


def _head_sha(clone: Path) -> str:
    env = _git_env()
    return _run("git", "rev-parse", "HEAD", cwd=clone, env=env).stdout.strip()


class SelfUpdateScannerBehaviorTests(TestCase):
    """Per-repo decision-ladder cases on real ``tmp_path`` git clones."""

    def setUp(self) -> None:
        # ``self.tmp`` is the per-test scratch dir for the bare origin + clones.
        self._tmp = Path(self._make_tempdir())
        self.origin = self._tmp / "origin.git"
        _seed_origin_with_two_commits(self.origin)

    def _make_tempdir(self) -> str:
        import tempfile  # noqa: PLC0415

        d = tempfile.mkdtemp(prefix="self_update_scanner_")
        self.addCleanup(_rmtree_safe, d)
        return d

    def _scanner(self, *, repos: list[tuple[str, Path]], cadence_hours: int = 1) -> SelfUpdateScanner:
        return SelfUpdateScanner(repos=tuple(repos), cadence_hours=cadence_hours)

    def test_clean_default_branch_with_trailing_commit_is_fast_forwarded(self) -> None:
        """The canonical success path: clone is one commit behind, scanner ff-pulls it."""
        clone = self._tmp / "teatree"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        origin_head = _run("git", "-C", str(self.origin), "rev-parse", "main", cwd=self._tmp).stdout.strip()
        assert old_sha != origin_head

        signals = self._scanner(repos=[("teatree", clone)]).scan()

        new_sha = _head_sha(clone)
        assert new_sha == origin_head, "scanner did not fast-forward the clone"
        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "self_update.updated"
        assert signal.payload["repo"] == "teatree"
        assert signal.payload["old_sha"].startswith(old_sha[:7])
        assert signal.payload["new_sha"].startswith(origin_head[:7])

    def test_already_up_to_date_is_recorded_but_emits_no_updated_signal(self) -> None:
        """Already-current clone yields a ``up_to_date`` signal, not ``updated``."""
        clone = self._tmp / "teatree"
        _clone_up_to_date(origin=self.origin, clone=clone)
        before = _head_sha(clone)

        signals = self._scanner(repos=[("teatree", clone)]).scan()

        assert _head_sha(clone) == before
        assert len(signals) == 1
        assert signals[0].kind == "self_update.up_to_date"

    def test_dirty_tracked_tree_is_skipped_with_warning(self) -> None:
        """Tracked modifications block the ff-pull (never clobber local work)."""
        clone = self._tmp / "teatree"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        _make_tracked_dirty(clone)

        signals = self._scanner(repos=[("teatree", clone)]).scan()

        assert _head_sha(clone) == old_sha, "scanner clobbered a dirty tree"
        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "self_update.skipped"
        assert "dirty" in signal.payload["reason"] or "tracked" in signal.payload["reason"]

    def test_feature_branch_checkout_is_skipped(self) -> None:
        """Non-default-branch checkouts are skipped — agent's work-in-flight is sacred."""
        clone = self._tmp / "teatree"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        _checkout_feature_branch(clone)

        signals = self._scanner(repos=[("teatree", clone)]).scan()

        assert _head_sha(clone) == old_sha
        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "self_update.skipped"
        assert "branch" in signal.payload["reason"].lower()

    def test_cadence_not_elapsed_skips_without_running_git(self) -> None:
        """A recent successful pull within the cadence window short-circuits the scan."""
        clone = self._tmp / "teatree"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        # Pre-record a marker observed 5 minutes ago — the 1-hour cadence has NOT elapsed.
        SelfUpdateMarker.objects.create(
            repo_label="teatree",
            repo_path=str(clone),
            last_outcome="updated",
            last_pulled_sha="deadbeef",
            last_pull_at=timezone.now() - _dt.timedelta(minutes=5),
        )
        old_sha = _head_sha(clone)

        signals = self._scanner(repos=[("teatree", clone)], cadence_hours=1).scan()

        # Clone was NOT touched — the cadence gate prevented the git work.
        assert _head_sha(clone) == old_sha
        assert len(signals) == 1
        assert signals[0].kind == "self_update.cadence_not_elapsed"

    def test_cadence_elapsed_runs_the_pull(self) -> None:
        """An old marker re-enables the per-tick pull."""
        clone = self._tmp / "teatree"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        SelfUpdateMarker.objects.create(
            repo_label="teatree",
            repo_path=str(clone),
            last_outcome="up_to_date",
            last_pulled_sha=old_sha,
            last_pull_at=timezone.now() - _dt.timedelta(hours=2),
        )

        signals = self._scanner(repos=[("teatree", clone)], cadence_hours=1).scan()

        assert _head_sha(clone) != old_sha
        assert len(signals) == 1
        assert signals[0].kind == "self_update.updated"

    def test_marker_persisted_after_successful_pull(self) -> None:
        """The post-pull marker records the new SHA + ``updated`` outcome."""
        clone = self._tmp / "teatree"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone)

        self._scanner(repos=[("teatree", clone)]).scan()

        marker = SelfUpdateMarker.objects.get(repo_label="teatree")
        assert marker.last_outcome == "updated"
        assert marker.last_pulled_sha == _head_sha(clone)
        assert (timezone.now() - marker.last_pull_at).total_seconds() < 60

    def test_marker_persisted_after_skip(self) -> None:
        """Even a skip writes a marker — the cadence gate needs *something* to read."""
        clone = self._tmp / "teatree"
        old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        _checkout_feature_branch(clone)

        self._scanner(repos=[("teatree", clone)]).scan()

        marker = SelfUpdateMarker.objects.get(repo_label="teatree")
        assert marker.last_outcome == "skipped"
        assert marker.last_pulled_sha == old_sha

    def test_per_repo_iteration_isolates_failures(self) -> None:
        """One repo's failure does not stop the scanner from processing siblings."""
        clone_a = self._tmp / "teatree"
        clone_b = self._tmp / "overlay-b"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone_a)
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone_b)
        _checkout_feature_branch(clone_a)  # A will skip; B should still pull.

        signals = self._scanner(repos=[("teatree", clone_a), ("overlay-b", clone_b)]).scan()

        kinds = sorted(s.kind for s in signals)
        assert kinds == ["self_update.skipped", "self_update.updated"]
        assert SelfUpdateMarker.objects.filter(repo_label="teatree").exists()
        assert SelfUpdateMarker.objects.filter(repo_label="overlay-b").exists()

    def test_missing_repo_path_emits_failed_signal(self) -> None:
        """A repo path that does not exist on disk is a fail, not a crash."""
        missing = self._tmp / "does-not-exist"

        signals = self._scanner(repos=[("teatree", missing)]).scan()

        assert len(signals) == 1
        assert signals[0].kind == "self_update.failed"

    def test_scanner_name_is_self_update(self) -> None:
        """The scanner identifies itself for the dispatcher's logs."""
        assert SelfUpdateScanner(repos=()).name == "self_update"


def _rmtree_safe(path: str) -> None:
    import shutil  # noqa: PLC0415

    shutil.rmtree(path, ignore_errors=True)


class SelfUpdateScannerWiringTests(TestCase):
    """The wiring layer reads the cadence setting and enumerates target repos."""

    def test_default_cadence_setting_is_one_hour(self) -> None:
        """``UserSettings.self_update_cadence_hours`` defaults to 1 hour."""
        from teatree.config import UserSettings  # noqa: PLC0415

        settings = UserSettings()
        assert settings.self_update_cadence_hours == 1

    def test_self_update_disabled_setting_defaults_off(self) -> None:
        """``self_update_disabled`` defaults to ``False`` — scanner is on by default."""
        from teatree.config import UserSettings  # noqa: PLC0415

        settings = UserSettings()
        assert settings.self_update_disabled is False

    def test_wiring_builds_scanner_when_repos_available(self) -> None:
        """The wiring helper returns a scanner with the configured cadence."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.tick_jobs import _self_update_scanner  # noqa: PLC0415

        with (
            patch(
                "teatree.loop.tick_jobs.load_config",
                return_value=type("Cfg", (), {"user": UserSettings(self_update_cadence_hours=3)})(),
            ),
            patch(
                "teatree.loop.tick_jobs._collect_self_update_repos",
                return_value=[("teatree", Path("/x/teatree"))],
            ),
        ):
            scanner = _self_update_scanner()
        assert scanner is not None
        assert scanner.cadence_hours == 3
        assert scanner.repos == (("teatree", Path("/x/teatree")),)

    def test_wiring_returns_none_when_disabled(self) -> None:
        """Escape hatch — ``self_update_disabled=True`` → no scanner."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.tick_jobs import _self_update_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.tick_jobs.load_config",
            return_value=type("Cfg", (), {"user": UserSettings(self_update_disabled=True)})(),
        ):
            scanner = _self_update_scanner()
        assert scanner is None

    def test_wiring_returns_none_when_no_repos(self) -> None:
        """No editable clones discovered → nothing to scan, no scanner needed."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.tick_jobs import _self_update_scanner  # noqa: PLC0415

        with (
            patch(
                "teatree.loop.tick_jobs.load_config",
                return_value=type("Cfg", (), {"user": UserSettings()})(),
            ),
            patch(
                "teatree.loop.tick_jobs._collect_self_update_repos",
                return_value=[],
            ),
        ):
            scanner = _self_update_scanner()
        assert scanner is None

    def test_build_default_jobs_includes_self_update_when_wired(self) -> None:
        """``build_default_jobs`` wires the self-update scanner as a global job."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.scanners.self_update import SelfUpdateScanner  # noqa: PLC0415
        from teatree.loop.tick_jobs import build_default_jobs  # noqa: PLC0415

        fake_scanner = SelfUpdateScanner(repos=(("teatree", Path("/x")),), cadence_hours=1)
        with patch(
            "teatree.loop.tick_jobs._self_update_scanner",
            return_value=fake_scanner,
        ):
            jobs = build_default_jobs()

        assert any(j.scanner is fake_scanner and j.overlay == "" for j in jobs)
