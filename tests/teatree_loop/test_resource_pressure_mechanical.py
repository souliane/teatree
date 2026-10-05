"""Tests for the safe resource-pressure freeing handler."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models.resource_pressure_marker import ResourcePressureMarker
from teatree.loop import mechanical_resources
from teatree.loop.mechanical_resources import free_resources

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_GIB = 1024 * 1024 * 1024


def _git_env() -> dict[str, str]:
    """Deterministic git env that lets ``commit`` succeed and ignores the outer GIT_*."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
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


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=cwd, env=_git_env(), capture_output=True, text=True, check=True)


def _write_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size_bytes)


class DiskCachePurgeTests(TestCase):
    """Only allow-listed cache dirs are purged; protected paths are never touched."""

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self.tmp = Path(tempfile.mkdtemp(prefix="rp_disk_"))
        self.addCleanup(_rmtree_safe, str(self.tmp))
        self.uv_prune = _patch_uv_cache_prune(self)

    def test_allowlisted_dir_is_removed(self) -> None:
        cache = self.tmp / "pre-commit"
        _write_file(cache / "blob", 1024)
        free_resources({"resource": "disk", "disk_cache_allowlist": [str(cache)]})
        assert not cache.exists()
        self.uv_prune.assert_called_once()

    def test_non_allowlisted_dir_is_untouched(self) -> None:
        listed = self.tmp / "puppeteer"
        unlisted = self.tmp / "prek"
        _write_file(listed / "blob", 1024)
        _write_file(unlisted / "blob", 1024)
        free_resources({"resource": "disk", "disk_cache_allowlist": [str(listed)]})
        assert not listed.exists()
        assert unlisted.exists(), "a cache NOT on the allow-list must survive"

    def test_protected_projects_path_is_refused_even_if_listed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        projects = self.tmp / "projects"
        _write_file(projects / "session.jsonl", 1024)
        with patch.object(mechanical_resources, "_PROTECTED_DISK_PATHS", (str(projects),)):
            free_resources({"resource": "disk", "disk_cache_allowlist": [str(projects)]})
        assert projects.exists(), "session memory must never be purged"

    def _purge_guarded(self, *, protected: Path, allowlisted: Path) -> None:
        with (
            patch.object(mechanical_resources, "_PROTECTED_DISK_PATHS", (str(protected),)),
            patch.object(mechanical_resources, "reclaim_disk", return_value=_fake_reclaim_report()),
        ):
            free_resources({"resource": "disk", "disk_cache_allowlist": [str(allowlisted)]})

    def test_ancestor_of_a_protected_path_is_refused(self) -> None:
        """``rmtree`` on an entry CONTAINING a protected path takes that path down with it."""
        home = self.tmp / "claude"
        projects = home / "projects"
        _write_file(projects / "session.jsonl", 1024)
        _write_file(home / "settings.json", 16)

        self._purge_guarded(protected=projects, allowlisted=home)

        assert projects.exists(), "an ancestor of session memory must never be purged"

    def test_descendant_of_a_protected_path_is_refused(self) -> None:
        """An entry INSIDE a protected path is part of the state that path protects."""
        projects = self.tmp / "projects"
        one_project = projects / "some-project"
        _write_file(one_project / "session.jsonl", 1024)

        self._purge_guarded(protected=projects, allowlisted=one_project)

        assert one_project.exists(), "a subtree of session memory must never be purged"

    def test_a_sibling_of_a_protected_path_is_still_purged(self) -> None:
        """Anti-vacuous control: containment must not swallow an ordinary neighbouring cache."""
        projects = self.tmp / "projects"
        cache = self.tmp / "prek"
        _write_file(projects / "session.jsonl", 1024)
        _write_file(cache / "blob", 1024)

        self._purge_guarded(protected=projects, allowlisted=cache)

        assert not cache.exists()
        assert projects.exists()

    def test_reclaimed_bytes_recorded_on_marker(self) -> None:
        cache = self.tmp / "pre-commit"
        _write_file(cache / "blob", _GIB // 2)
        free_resources({"resource": "disk", "disk_cache_allowlist": [str(cache)]})
        marker = ResourcePressureMarker.load()
        assert marker.last_plan
        assert "PURGE cache" in marker.last_plan
        assert marker.last_freed_at is not None

    def test_an_absent_allowlist_entry_is_named_in_the_plan(self) -> None:
        """A stale allow-list entry must be VISIBLE, not silently worth 0.00 GB (#3852).

        Every entry of the shipped default was absent on the host that produced
        this ticket (``pre-commit`` had been replaced by ``prek``, and neither
        puppeteer nor codex was installed), so each CRITICAL tick purged nothing
        and reported the same "~0.00 GB" a genuinely-clean cache produces. The
        alarm therefore re-fired forever with no way to tell why.
        """
        free_resources({"resource": "disk", "disk_cache_allowlist": [str(self.tmp / "never-installed")]})

        plan = ResourcePressureMarker.load().last_plan
        assert "SKIP cache" in plan
        assert "absent" in plan
        assert "never-installed" in plan

    def test_a_present_entry_is_still_a_purge_not_a_skip(self) -> None:
        """Anti-vacuous control: the SKIP line must not swallow the real purge case."""
        cache = self.tmp / "pre-commit"
        _write_file(cache / "blob", 1024)

        free_resources({"resource": "disk", "disk_cache_allowlist": [str(cache)]})

        plan = ResourcePressureMarker.load().last_plan
        assert "PURGE cache" in plan
        assert "SKIP cache" not in plan


class DiskDockerReclaimTests(TestCase):
    """Under disk pressure the ladder reaps Docker build cache + dangling/unused images.

    Build cache and unused images are typically the largest reclaimable
    consumers, and the file-cache purge does not touch them. The disk freeing
    pass routes the safety-vetted ``reclaim_disk`` (build cache + DANGLING-only
    images + UNREFERENCED-only volumes, never ``-a``), so a running container's
    images can never be removed.
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self.tmp = Path(tempfile.mkdtemp(prefix="rp_docker_"))
        self.addCleanup(_rmtree_safe, str(self.tmp))
        self.uv_prune = _patch_uv_cache_prune(self)

    def test_disk_plan_includes_docker_reclaim_step(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "reclaim_disk") as mock_reclaim:
            mock_reclaim.return_value = _fake_reclaim_report()
            free_resources({"resource": "disk", "disk_cache_allowlist": []})
        marker = ResourcePressureMarker.load()
        assert "RECLAIM docker" in marker.last_plan

    def test_disk_freeing_invokes_safe_reclaim(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "reclaim_disk") as mock_reclaim:
            mock_reclaim.return_value = _fake_reclaim_report()
            free_resources({"resource": "disk", "disk_cache_allowlist": []})
        mock_reclaim.assert_called_once()

    def test_reclaimed_docker_bytes_counted_in_plan(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "reclaim_disk") as mock_reclaim:
            mock_reclaim.return_value = _fake_reclaim_report(total_bytes=37 * _GIB)
            free_resources({"resource": "disk", "disk_cache_allowlist": []})
        marker = ResourcePressureMarker.load()
        assert "RECLAIM docker" in marker.last_plan
        assert marker.last_freed_at is not None

    def test_docker_reclaim_failure_is_swallowed(self) -> None:
        """A docker prune failure never crashes the freeing pass."""
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "reclaim_disk", side_effect=RuntimeError("docker down")):
            free_resources({"resource": "disk", "disk_cache_allowlist": []})  # must not raise

    def test_plan_says_the_reclaim_did_not_run_when_the_venue_cannot_act(self) -> None:
        """A 0B reclaim line in the persisted plan reads as "nothing to reclaim" (#4585)."""
        with patch.object(mechanical_resources, "reclaim_disk") as mock_reclaim:
            mock_reclaim.return_value = _venue_blocked_reclaim_report()
            free_resources({"resource": "disk", "disk_cache_allowlist": []})
        plan = ResourcePressureMarker.load().last_plan
        assert "did not run" in plan
        assert "permission denied" in plan
        assert "docker reclaimed 0B" not in plan

    def test_ram_ladder_does_not_invoke_disk_reclaim(self) -> None:
        with (
            patch.object(mechanical_resources, "_idle_containers", return_value=[]),
            patch.object(mechanical_resources, "_docker_container_prune"),
            patch.object(mechanical_resources, "reclaim_disk") as mock_reclaim,
        ):
            free_resources({"resource": "ram"})
        mock_reclaim.assert_not_called()

    def test_reclaim_uses_only_zero_dataloss_argv(self) -> None:
        """The reclaim set never contains ``-a`` / ``system prune`` (running images survive).

        This pins the safety boundary at the resource_pressure call site: the
        ladder uses the sanctioned ``reclaim_disk`` whose fixed argv can never
        reap a running container's images. Asserted against the REAL reclaim
        plan, not a mock.
        """
        from teatree.docker.reclaim import reclaim_disk  # noqa: PLC0415

        report = reclaim_disk(dry_run=True)
        argvs = [" ".join(step.argv) for step in report.planned]
        assert any("builder prune" in a for a in argvs), "build cache must be in the reclaim set"
        for argv in argvs:
            assert "-a" not in argv.split(), f"reclaim must never use -a (would reap running images): {argv}"
            assert "--all" not in argv, f"reclaim must never use --all: {argv}"
            assert "system" not in argv.split(), f"reclaim must never use `system prune`: {argv}"


class DryRunFirstTests(TestCase):
    """The plan is persisted before execution, and recorded even when flags are off."""

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self.tmp = Path(tempfile.mkdtemp(prefix="rp_dryrun_"))
        self.addCleanup(_rmtree_safe, str(self.tmp))
        self.uv_prune = _patch_uv_cache_prune(self)

    def test_plan_persisted_before_execution(self) -> None:
        """Even if execution fails midway, the pre-execution plan is on the marker."""
        cache = self.tmp / "pre-commit"
        _write_file(cache / "blob", 1024)
        with (
            patch.object(mechanical_resources, "_run_uv_cache_prune", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            free_resources({"resource": "disk", "disk_cache_allowlist": [str(cache)]})
        marker = ResourcePressureMarker.load()
        assert "PURGE cache" in marker.last_plan


class RamLadderTests(TestCase):
    def test_idle_containers_are_stopped(self) -> None:
        calls: list[list[str]] = []

        def fake_docker(*args: str) -> str | None:
            calls.append(list(args))
            if args[:2] == ("ps", "-a"):
                return "abc123\ndef456\n"
            return ""

        with (
            patch.object(mechanical_resources.shutil, "which", return_value="/usr/bin/docker"),
            patch.object(mechanical_resources, "_docker", side_effect=fake_docker),
        ):
            free_resources({"resource": "ram"})
        assert ["stop", "abc123"] in calls
        assert ["stop", "def456"] in calls
        assert ["container", "prune", "-f"] in calls


class DoneWorktreeSweepTests(TestCase):
    def setUp(self) -> None:
        self.uv_prune = _patch_uv_cache_prune(self)

    def test_the_disk_ladder_runs_the_done_worktree_sweep(self) -> None:
        with (
            patch.object(mechanical_resources, "reclaim_disk", return_value=_fake_reclaim_report()),
            patch("teatree.core.worktree.worktree_done.reap_done_worktrees", return_value=["reaped one"]) as mock_reap,
        ):
            free_resources({"resource": "disk", "disk_cache_allowlist": []})
        mock_reap.assert_called_once()
        assert mock_reap.call_args.kwargs == {"dry_run": False}
        assert "done-worktree sweep handled 1 worktree row(s)" in ResourcePressureMarker.load().last_plan


class ReclaimStallTests(TestCase):
    def setUp(self) -> None:
        self.uv_prune = _patch_uv_cache_prune(self)
        patcher = patch.object(mechanical_resources, "_reap_done_worktrees")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pass(self, *, reclaimed_gb: float = 0.0, free_gb: float = 0.2) -> None:
        with patch.object(mechanical_resources, "_reclaim_docker_disk", return_value=reclaimed_gb):
            free_resources(
                {
                    "resource": "disk",
                    "disk_cache_allowlist": [],
                    "free_gb": free_gb,
                    "disk_warn_free_gb": 25.0,
                    "disk_crit_free_gb": 10.0,
                },
            )

    def test_a_zero_yield_pass_below_the_floor_increments_the_streak_and_names_it_in_the_plan(self) -> None:
        self._pass()
        assert ResourcePressureMarker.load().zero_yield_passes == 1

        self._pass()
        self._pass()

        marker = ResourcePressureMarker.load()
        assert marker.zero_yield_passes == 3
        assert "STALLED disk reclaim" in marker.last_plan

    def test_a_pass_that_frees_bytes_resets_the_streak(self) -> None:
        self._pass()
        self._pass()

        self._pass(reclaimed_gb=1.0)

        marker = ResourcePressureMarker.load()
        assert marker.zero_yield_passes == 0
        assert "STALLED disk reclaim" not in marker.last_plan

    def test_a_zero_yield_pass_with_room_to_spare_is_not_a_stall(self) -> None:
        self._pass(free_gb=200.0)

        assert ResourcePressureMarker.load().zero_yield_passes == 0


class ResilienceTests(TestCase):
    """Per-step failures are isolated; a failed pass raises for the tick to record."""

    def test_unknown_resource_is_noop(self) -> None:
        free_resources({"resource": "nonsense"})  # must not raise

    def test_a_failed_pass_raises_for_the_tick_to_record(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_plan_disk", side_effect=RuntimeError("kaboom")),
            pytest.raises(RuntimeError, match="kaboom"),
        ):
            free_resources({"resource": "disk"})


class HelperTests(TestCase):
    """Pure helpers — size accounting, path containment, process parsing, fail-open shells."""

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self.tmp = Path(tempfile.mkdtemp(prefix="rp_help_"))
        self.addCleanup(_rmtree_safe, str(self.tmp))

    def test_dir_size_counts_nested_files(self) -> None:
        _write_file(self.tmp / "a" / "f1", 1024)
        _write_file(self.tmp / "b" / "f2", 2048)
        assert mechanical_resources._dir_size_gb(str(self.tmp)) == pytest.approx(3072 / _GIB)

    def test_dir_size_of_missing_path_is_zero(self) -> None:
        assert mechanical_resources._dir_size_gb(str(self.tmp / "nope")) == pytest.approx(0.0)

    def test_purge_missing_dir_is_zero(self) -> None:
        assert mechanical_resources._purge_dir(str(self.tmp / "nope")) == pytest.approx(0.0)

    def test_clean_stale_statusline_removes_old_files(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        base = self.tmp / "statusline"
        fresh = base / "fresh"
        stale = base / "stale"
        _write_file(fresh, 10)
        _write_file(stale, 10)
        os.utime(stale, (1_600_000_000, 1_600_000_000))
        with patch.object(mechanical_resources, "_STATUSLINE_DIR", base):
            mechanical_resources._clean_stale_statusline()
        assert fresh.exists()
        assert not stale.exists()

    def test_docker_returns_none_without_binary(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.shutil, "which", return_value=None):
            assert mechanical_resources._docker("ps") is None

    def test_run_maps_nonzero_exit_to_none(self) -> None:
        from subprocess import CompletedProcess  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(
            mechanical_resources,
            "run_allowed_to_fail",
            return_value=CompletedProcess(args=["x"], returncode=2, stdout="out", stderr=""),
        ):
            assert mechanical_resources._run(["/bin/x"]) is None

    def test_run_maps_oserror_to_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "run_allowed_to_fail", side_effect=OSError):
            assert mechanical_resources._run(["/bin/x"]) is None

    def test_run_maps_unexpected_exception_to_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "run_allowed_to_fail", side_effect=RuntimeError("weird")):
            assert mechanical_resources._run(["/bin/x"]) is None

    def test_run_returns_stdout_on_success(self) -> None:
        from subprocess import CompletedProcess  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(
            mechanical_resources,
            "run_allowed_to_fail",
            return_value=CompletedProcess(args=["x"], returncode=0, stdout="hi", stderr=""),
        ):
            assert mechanical_resources._run(["/bin/x"]) == "hi"

    def test_persist_plan_failure_is_swallowed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.loop.mechanical_plan import FreePlan, persist_plan  # noqa: PLC0415 — deferred

        plan = FreePlan(resource="disk", steps=["x"])
        marker = ResourcePressureMarker.load()
        with patch.object(type(marker), "save", side_effect=RuntimeError("db")):
            # must not raise
            persist_plan(marker, plan, field_name="last_plan", caller="free_resources")

    def test_uv_cache_prune_noop_without_binary(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.shutil, "which", return_value=None):
            mechanical_resources._run_uv_cache_prune()  # must not raise

    def test_resolve_allowlist_skips_unresolvable_path(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        original_resolve = Path.resolve
        marker = self.tmp / "unresolvable"

        def selective_resolve(self_path: Path, *a: object, **k: object) -> Path:
            if self_path == marker.expanduser():
                raise OSError
            return original_resolve(self_path, *a, **k)

        with patch.object(mechanical_resources.Path, "resolve", new=selective_resolve):
            resolved = mechanical_resources._resolve_disk_allowlist({"disk_cache_allowlist": [str(marker)]})
        assert resolved == []

    def test_purge_dir_rmtree_failure_returns_zero(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        cache = self.tmp / "c"
        _write_file(cache / "f", 1024)
        with patch.object(mechanical_resources.shutil, "rmtree", side_effect=OSError):
            assert mechanical_resources._purge_dir(str(cache)) == pytest.approx(0.0)

    def test_dir_size_skips_unstattable_file(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        _write_file(self.tmp / "f", 1024)
        original_stat = Path.stat

        def stat_raises_for_named_file(self_path: Path, *a: object, **k: object) -> object:
            if self_path.name == "f":
                raise OSError
            return original_stat(self_path, *a, **k)

        with patch.object(mechanical_resources.Path, "stat", new=stat_raises_for_named_file):
            assert mechanical_resources._dir_size_gb(str(self.tmp)) == pytest.approx(0.0)

    def test_clean_stale_statusline_noop_when_dir_absent(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_STATUSLINE_DIR", self.tmp / "absent"):
            mechanical_resources._clean_stale_statusline()  # must not raise

    def test_idle_containers_empty_when_docker_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_docker", return_value=None):
            assert mechanical_resources._idle_containers() == []

    def test_clean_stale_statusline_swallows_unlink_error(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        base = self.tmp / "statusline"
        stale = base / "stale"
        _write_file(stale, 10)
        os.utime(stale, (1_600_000_000, 1_600_000_000))
        with (
            patch.object(mechanical_resources, "_STATUSLINE_DIR", base),
            patch.object(mechanical_resources.Path, "unlink", side_effect=OSError),
        ):
            mechanical_resources._clean_stale_statusline()  # must not raise
        assert stale.exists(), "unlink failed (swallowed) — file remains"

    def test_docker_invokes_run_when_binary_present(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources.shutil, "which", return_value="/usr/bin/docker"),
            patch.object(mechanical_resources, "_run", return_value="abc\n") as mock_run,
        ):
            assert mechanical_resources._docker("ps") == "abc\n"
        mock_run.assert_called_once()


def _fake_reclaim_report(total_bytes: int = 0) -> object:
    """A stand-in ``ReclaimReport`` whose ``total_bytes`` the ladder reads."""
    from teatree.docker.reclaim import PruneOutcome, ReclaimReport, ReclaimStep  # noqa: PLC0415

    step = ReclaimStep(
        argv=["docker", "builder", "prune", "-af"],
        label="build cache",
        outcome=PruneOutcome(reclaimed="x", bytes_reclaimed=total_bytes),
    )
    return ReclaimReport(steps=(step,), planned=(step,), dry_run=False)


def _venue_blocked_reclaim_report() -> object:
    """The socket-less service: nothing ran, so the plan must not read as a 0B reclaim."""
    from teatree.docker.reclaim import ReclaimReport, ReclaimStep  # noqa: PLC0415 — lazy, as _fake_reclaim_report above
    from teatree.docker.venue import DockerVenue  # noqa: PLC0415 — lazy, as _fake_reclaim_report above

    step = ReclaimStep(argv=["docker", "builder", "prune", "-af"], label="build cache")
    venue = DockerVenue(reachable=False, reason="permission denied", containerized=True, service_role="admin")
    return ReclaimReport(steps=(), planned=(step,), dry_run=False, venue=venue)


def _patch_uv_cache_prune(case: TestCase) -> MagicMock:
    """No-op the real ``uv cache prune`` sink for a disk-path TestCase.

    ``free_resources({"resource": "disk", ...})`` reaches ``_run_uv_cache_prune``,
    which shells out to a real ``uv cache prune``. That walk over a warmed uv
    cache (``setup-uv enable-cache`` in CI) exceeds the pytest-timeout — the
    real-subprocess timeouts that red the ``test-shuffle`` lane. The real
    function returns ``None``, so the patch mirrors that. Returns the mock so a
    test can ``assert_called_once()`` that the sink was invoked.
    """
    patcher = patch.object(mechanical_resources, "_run_uv_cache_prune", return_value=None)
    mock = patcher.start()
    case.addCleanup(patcher.stop)
    return mock


def _rmtree_safe(path: str) -> None:
    import shutil  # noqa: PLC0415

    shutil.rmtree(path, ignore_errors=True)
