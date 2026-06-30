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

from teatree.core.models.pending_reinstall import PendingReinstall
from teatree.core.models.self_update_marker import SelfUpdateMarker
from teatree.loop.scanners.self_update import SelfUpdateScanner
from teatree.loop.scanners.self_update_ci import CiVerdict, MainCiStatus

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class _StubCiStatus(MainCiStatus):
    """Inject a fixed CI verdict so the gate is exercised without ``gh``."""

    def __init__(self, verdict: CiVerdict) -> None:
        self._verdict = verdict
        self.queried: list[Path] = []

    def verdict(self, *, repo: Path) -> CiVerdict:
        self.queried.append(repo)
        return self._verdict


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
        # Default to a GREEN CI verdict so these decision-ladder tests exercise
        # the pull path; the CI-gate fail-closed cases live in their own class.
        return SelfUpdateScanner(
            repos=tuple(repos),
            cadence_hours=cadence_hours,
            ci_status=_StubCiStatus(CiVerdict.GREEN),
        )

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


class SelfUpdateCiGateTests(TestCase):
    """#1760: the CI-green fail-closed gate — only an explicit green ff-pulls."""

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self._tmp = Path(tempfile.mkdtemp(prefix="self_update_ci_gate_"))
        self.addCleanup(_rmtree_safe, str(self._tmp))
        self.origin = self._tmp / "origin.git"
        _seed_origin_with_two_commits(self.origin)
        self.clone = self._tmp / "teatree"
        self.old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=self.clone)

    def _scan(self, *, ci_status: MainCiStatus | None, require_green_main: bool = True) -> list:
        scanner = SelfUpdateScanner(
            repos=(("teatree", self.clone),),
            ci_status=ci_status,
            require_green_main=require_green_main,
        )
        return scanner.scan()

    def test_green_ci_proceeds_with_the_pull(self) -> None:
        signals = self._scan(ci_status=_StubCiStatus(CiVerdict.GREEN))

        assert _head_sha(self.clone) != self.old_sha
        assert signals[0].kind == "self_update.updated"

    def test_red_ci_skips_fail_closed(self) -> None:
        signals = self._scan(ci_status=_StubCiStatus(CiVerdict.RED))

        assert _head_sha(self.clone) == self.old_sha, "a red default branch must NOT be pulled"
        assert signals[0].kind == "self_update.skipped"
        assert signals[0].payload["reason"] == "ci_red"

    def test_pending_ci_skips_fail_closed(self) -> None:
        signals = self._scan(ci_status=_StubCiStatus(CiVerdict.PENDING))

        assert _head_sha(self.clone) == self.old_sha
        assert signals[0].payload["reason"] == "ci_pending"

    def test_unknown_ci_skips_fail_closed(self) -> None:
        signals = self._scan(ci_status=_StubCiStatus(CiVerdict.UNKNOWN))

        assert _head_sha(self.clone) == self.old_sha
        assert signals[0].payload["reason"] == "ci_unknown"

    def test_missing_ci_source_with_gate_on_skips_unknown(self) -> None:
        # require_green_main on but no CI source configured → still fail-closed.
        signals = self._scan(ci_status=None)

        assert _head_sha(self.clone) == self.old_sha
        assert signals[0].payload["reason"] == "ci_unknown"

    def test_gate_off_pulls_without_querying_ci(self) -> None:
        ci = _StubCiStatus(CiVerdict.RED)  # would block if consulted
        signals = self._scan(ci_status=ci, require_green_main=False)

        assert _head_sha(self.clone) != self.old_sha, "gate off must pull regardless of CI"
        assert signals[0].kind == "self_update.updated"
        assert ci.queried == [], "gate off must not query the CI source at all"

    def test_ci_not_queried_when_clone_already_up_to_date(self) -> None:
        # An already-current clone is up_to_date BEFORE the CI gate — no remote call.
        up_to_date = self._tmp / "current"
        _clone_up_to_date(origin=self.origin, clone=up_to_date)
        ci = _StubCiStatus(CiVerdict.RED)

        signals = SelfUpdateScanner(repos=(("teatree", up_to_date),), ci_status=ci).scan()

        assert signals[0].kind == "self_update.up_to_date"
        assert ci.queried == [], "CI must not be queried on the up-to-date common path"

    def test_dirty_tree_skipped_before_ci_is_queried(self) -> None:
        _make_tracked_dirty(self.clone)
        ci = _StubCiStatus(CiVerdict.GREEN)

        signals = SelfUpdateScanner(repos=(("teatree", self.clone),), ci_status=ci).scan()

        assert signals[0].kind == "self_update.skipped"
        assert "dirty" in signals[0].payload["reason"] or "tracked" in signals[0].payload["reason"]
        assert ci.queried == [], "the dirty-tree skip must precede the CI query"


class SelfUpdateDeferredReinstallQueueTests(TestCase):
    """#1760: ``auto_update_reinstall`` queues a deferred reinstall on update only."""

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self._tmp = Path(tempfile.mkdtemp(prefix="self_update_reinstall_q_"))
        self.addCleanup(_rmtree_safe, str(self._tmp))
        self.origin = self._tmp / "origin.git"
        _seed_origin_with_two_commits(self.origin)
        self.clone = self._tmp / "teatree"
        self.old_sha = _clone_trailing_by_one_commit(origin=self.origin, clone=self.clone)

    def _scanner(self, *, auto_update_reinstall: bool) -> SelfUpdateScanner:
        return SelfUpdateScanner(
            repos=(("teatree", self.clone),),
            ci_status=_StubCiStatus(CiVerdict.GREEN),
            auto_update_reinstall=auto_update_reinstall,
        )

    def test_update_queues_pending_reinstall_when_opted_in(self) -> None:
        self._scanner(auto_update_reinstall=True).scan()

        row = PendingReinstall.objects.get(repo_label="teatree")
        assert row.state == PendingReinstall.State.PENDING
        assert row.target_sha == _head_sha(self.clone)

    def test_update_does_not_queue_when_flag_off(self) -> None:
        self._scanner(auto_update_reinstall=False).scan()

        assert not PendingReinstall.objects.filter(repo_label="teatree").exists()

    def test_no_queue_when_nothing_advanced(self) -> None:
        current = self._tmp / "current"
        _clone_up_to_date(origin=self.origin, clone=current)
        scanner = SelfUpdateScanner(
            repos=(("teatree", current),),
            ci_status=_StubCiStatus(CiVerdict.GREEN),
            auto_update_reinstall=True,
        )

        scanner.scan()

        assert not PendingReinstall.objects.filter(repo_label="teatree").exists()

    def test_queue_db_error_does_not_crash_the_tick(self) -> None:
        # A DB error while upserting the deferred-reinstall row must be
        # swallowed — the update itself already succeeded; the worst case is
        # one re-pull next tick, never a crashed tick.
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(
            PendingReinstall.objects,
            "upsert_pending",
            side_effect=RuntimeError("db gone"),
        ):
            signals = self._scanner(auto_update_reinstall=True).scan()

        assert signals[0].kind == "self_update.updated", "the tick must still report the update"
        assert _head_sha(self.clone) != self.old_sha


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

    def test_require_green_main_defaults_on(self) -> None:
        """``auto_update_require_green_main`` defaults ON — fail closed (#1760)."""
        from teatree.config import UserSettings  # noqa: PLC0415

        assert UserSettings().auto_update_require_green_main is True

    def test_auto_update_reinstall_defaults_off(self) -> None:
        """``auto_update_reinstall`` defaults OFF — the new side-effect is opt-in (#1760)."""
        from teatree.config import UserSettings  # noqa: PLC0415

        assert UserSettings().auto_update_reinstall is False

    def test_wiring_passes_ci_gate_and_reinstall_flags(self) -> None:
        """The wiring helper plumbs the CI source + both #1760 flags into the scanner."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _self_update_scanner  # noqa: PLC0415
        from teatree.loop.scanners.self_update_ci import GhMainCiStatus  # noqa: PLC0415

        settings = UserSettings(auto_update_reinstall=True, auto_update_require_green_main=False)
        with (
            patch(
                "teatree.loop.global_scanner_factories.load_config",
                return_value=type("Cfg", (), {"user": settings})(),
            ),
            patch(
                "teatree.loop.global_scanner_factories._collect_self_update_repos",
                return_value=[("teatree", Path("/x/teatree"))],
            ),
        ):
            scanner = _self_update_scanner()
        assert scanner is not None
        assert isinstance(scanner.ci_status, GhMainCiStatus)
        assert scanner.require_green_main is False
        assert scanner.auto_update_reinstall is True

    def test_wiring_builds_scanner_when_repos_available(self) -> None:
        """The wiring helper returns a scanner with the configured cadence."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _self_update_scanner  # noqa: PLC0415

        with (
            patch(
                "teatree.loop.global_scanner_factories.load_config",
                return_value=type("Cfg", (), {"user": UserSettings(self_update_cadence_hours=3)})(),
            ),
            patch(
                "teatree.loop.global_scanner_factories._collect_self_update_repos",
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
        from teatree.loop.global_scanner_factories import _self_update_scanner  # noqa: PLC0415

        with patch(
            "teatree.loop.global_scanner_factories.load_config",
            return_value=type("Cfg", (), {"user": UserSettings(self_update_disabled=True)})(),
        ):
            scanner = _self_update_scanner()
        assert scanner is None

    def test_wiring_returns_none_when_no_repos(self) -> None:
        """No editable clones discovered → nothing to scan, no scanner needed."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.config import UserSettings  # noqa: PLC0415
        from teatree.loop.global_scanner_factories import _self_update_scanner  # noqa: PLC0415

        with (
            patch(
                "teatree.loop.global_scanner_factories.load_config",
                return_value=type("Cfg", (), {"user": UserSettings()})(),
            ),
            patch(
                "teatree.loop.global_scanner_factories._collect_self_update_repos",
                return_value=[],
            ),
        ):
            scanner = _self_update_scanner()
        assert scanner is None

    def test_build_default_jobs_includes_self_update_when_wired(self) -> None:
        """``build_default_jobs`` wires the self-update scanner as a global job."""
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.global_scanner_factories import build_default_jobs  # noqa: PLC0415
        from teatree.loop.scanners.self_update import SelfUpdateScanner  # noqa: PLC0415

        fake_scanner = SelfUpdateScanner(repos=(("teatree", Path("/x")),), cadence_hours=1)
        with patch(
            "teatree.loop.global_scanner_factories._self_update_scanner",
            return_value=fake_scanner,
        ):
            jobs = build_default_jobs()

        assert any(j.scanner is fake_scanner and j.overlay == "" for j in jobs)


class SelfUpdateScannerStaleNoticeTests(TestCase):
    """A skipped dirty/detached clone emits a DURABLE user-facing notice (#2836).

    The incident was a SILENT skip — the clone went stale and ``t3`` ran old
    code. The scanner now routes a dirty / off-default skip through
    ``notify_stale_clone_skip`` (a BotPing-backed bot→user DM). These tests spy
    on that helper so they assert the durable-notice CONTRACT hermetically,
    without resolving a real messaging backend. Anti-vacuity: the up-to-date
    case proves a healthy clone emits NO notice (revert the ``scan`` wiring and
    the dirty/feature cases go RED).
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self._tmp = Path(tempfile.mkdtemp(prefix="self_update_stale_notice_"))
        self.addCleanup(_rmtree_safe, str(self._tmp))
        self.origin = self._tmp / "origin.git"
        _seed_origin_with_two_commits(self.origin)

    def _scanner(self, clone: Path) -> SelfUpdateScanner:
        return SelfUpdateScanner(
            repos=(("teatree", clone),),
            cadence_hours=1,
            ci_status=_StubCiStatus(CiVerdict.GREEN),
        )

    def test_dirty_clone_skip_emits_durable_notice(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.stale_clone_notice import StaleCloneReason  # noqa: PLC0415

        clone = self._tmp / "teatree"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        _make_tracked_dirty(clone)

        with patch("teatree.core.stale_clone_notice.notify_stale_clone_skip") as spy:
            self._scanner(clone).scan()

        spy.assert_called_once()
        skip = spy.call_args.args[0]
        assert skip.reason is StaleCloneReason.DIRTY
        assert skip.repo_path == str(clone)
        assert skip.label == "teatree"

    def test_feature_branch_skip_emits_off_default_notice(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.stale_clone_notice import StaleCloneReason  # noqa: PLC0415

        clone = self._tmp / "teatree"
        _clone_trailing_by_one_commit(origin=self.origin, clone=clone)
        _checkout_feature_branch(clone)

        with patch("teatree.core.stale_clone_notice.notify_stale_clone_skip") as spy:
            self._scanner(clone).scan()

        spy.assert_called_once()
        skip = spy.call_args.args[0]
        assert skip.reason is StaleCloneReason.OFF_DEFAULT
        assert skip.default_branch == "main"

    def test_healthy_clone_emits_no_notice(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        clone = self._tmp / "teatree"
        _clone_up_to_date(origin=self.origin, clone=clone)

        with patch("teatree.core.stale_clone_notice.notify_stale_clone_skip") as spy:
            self._scanner(clone).scan()

        spy.assert_not_called()
