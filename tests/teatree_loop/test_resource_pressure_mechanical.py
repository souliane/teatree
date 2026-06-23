"""Tests for ``free_resources`` — the resource-pressure freeing handler (#128).

The handler is the only place that *acts* on a CRITICAL pressure signal. The
safety-critical guarantees pinned here: allow-LIST cache purge (only listed
dirs are removed, ``~/.claude/projects`` and ``~/.cache/prek`` analogues are
never touched); dry-run / log-first (the plan is persisted before execution
and recorded even when a destructive flag is off); worktree GC is flag-gated
to clean + fully-pushed + stale worktrees only, never a dirty /
ahead-of-upstream / active-session worktree; process kill is flag-gated to
SIGTERM (never SIGKILL), allow-list only, never a session-ancestry pid, and
fires nothing below 2 consecutive ticks; and best-effort throughout (a
subprocess failure never crashes the tick).

Real filesystem + real ``git`` under ``tmp_path`` for the worktree cases;
``docker``/``ps``/``os.kill`` (third-party + irreversible externals) are
mocked. The marker, allow-list logic, and plan persistence are exercised
against the real ORM + real handler code.
"""

import os
import signal
import subprocess
from pathlib import Path

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

    def test_allowlisted_dir_is_removed(self) -> None:
        cache = self.tmp / "pre-commit"
        _write_file(cache / "blob", 1024)
        free_resources({"resource": "disk", "disk_cache_allowlist": [str(cache)]})
        assert not cache.exists()

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

    def test_reclaimed_bytes_recorded_on_marker(self) -> None:
        cache = self.tmp / "pre-commit"
        _write_file(cache / "blob", _GIB // 2)
        free_resources({"resource": "disk", "disk_cache_allowlist": [str(cache)]})
        marker = ResourcePressureMarker.load()
        assert marker.last_plan
        assert "PURGE cache" in marker.last_plan
        assert marker.last_freed_at is not None


class DiskDockerReclaimTests(TestCase):
    """Under disk pressure the ladder reaps Docker build cache + dangling/unused images.

    The disk-full incident: the build cache (15.5 GB) + unused images (~22 GB)
    were the real ~37 GB hog, and the disk ladder only purged file caches +
    stopped idle containers (RAM ladder) — it never reaped Docker disk. The fix
    routes the safety-vetted ``reclaim_disk`` (build cache + DANGLING-only images
    + UNREFERENCED-only volumes, never ``-a``) into the disk freeing pass, so a
    running container's images can never be removed.
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self.tmp = Path(tempfile.mkdtemp(prefix="rp_docker_"))
        self.addCleanup(_rmtree_safe, str(self.tmp))

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

    def test_ram_ladder_does_not_invoke_disk_reclaim(self) -> None:
        """The Docker disk reclaim belongs to the disk ladder, not the RAM ladder."""
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_idle_containers", return_value=[]),
            patch.object(mechanical_resources, "_docker_container_prune"),
            patch.object(mechanical_resources, "reclaim_disk") as mock_reclaim,
        ):
            free_resources({"resource": "ram", "allow_destructive_ram": False})
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

    def test_worktree_gc_off_records_skip_in_plan(self) -> None:
        free_resources({"resource": "disk", "disk_cache_allowlist": [], "allow_destructive_disk": False})
        marker = ResourcePressureMarker.load()
        assert "SKIP worktree GC (allow_destructive_disk=false)" in marker.last_plan

    def test_plan_persisted_before_execution(self) -> None:
        """Even if execution fails midway, the pre-execution plan is on the marker."""
        from unittest.mock import patch  # noqa: PLC0415

        cache = self.tmp / "pre-commit"
        _write_file(cache / "blob", 1024)
        with patch.object(mechanical_resources, "_run_uv_cache_prune", side_effect=RuntimeError("boom")):
            free_resources({"resource": "disk", "disk_cache_allowlist": [str(cache)]})
        marker = ResourcePressureMarker.load()
        # The failure was swallowed; the plan that was persisted first survives.
        assert "PURGE cache" in marker.last_plan


class WorktreeGcSafetyTests(TestCase):
    """Flag-gated worktree GC removes only clean + pushed + stale + non-session worktrees."""

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415

        self.tmp = Path(tempfile.mkdtemp(prefix="rp_wt_"))
        self.addCleanup(_rmtree_safe, str(self.tmp))
        self.origin = self.tmp / "origin.git"
        self._seed_origin()

    def _seed_origin(self) -> None:
        seed = self.tmp / "_seed"
        seed.mkdir()
        _run("git", "init", "--initial-branch=main", str(seed), cwd=self.tmp)
        (seed / "a.txt").write_text("a")
        _run("git", "add", "a.txt", cwd=seed)
        _run("git", "commit", "-m", "first", cwd=seed)
        _run("git", "init", "--bare", "--initial-branch=main", str(self.origin), cwd=self.tmp)
        _run("git", "remote", "add", "origin", str(self.origin), cwd=seed)
        _run("git", "push", "-u", "origin", "main", cwd=seed)
        self.main_clone = self.tmp / "main_clone"
        _run("git", "clone", str(self.origin), str(self.main_clone), cwd=self.tmp)

    def _add_worktree(self, name: str, branch: str) -> Path:
        wt = self.tmp / name
        _run("git", "worktree", "add", "-b", branch, str(wt), "main", cwd=self.main_clone)
        _run("git", "push", "-u", "origin", branch, cwd=wt)
        return wt

    def _make_stale(self, wt: Path) -> None:
        old = 1_600_000_000  # well over 30 days ago
        os.utime(wt, (old, old))

    def _payload(self) -> dict:
        return {
            "resource": "disk",
            "disk_cache_allowlist": [],
            "allow_destructive_disk": True,
            "worktree_stale_days": 30,
            "max_worktree_gc_per_tick": 5,
        }

    def test_clean_pushed_stale_worktree_is_removed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        wt = self._add_worktree("clean", "feat-clean")
        self._make_stale(wt)
        with patch.object(mechanical_resources, "workspace_dir", return_value=self.main_clone):
            free_resources(self._payload())
        assert not wt.exists(), "a clean+pushed+stale worktree should be GC'd"

    def test_dirty_worktree_is_skipped(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        wt = self._add_worktree("dirty", "feat-dirty")
        (wt / "a.txt").write_text("locally modified")  # tracked-dirty
        self._make_stale(wt)
        with patch.object(mechanical_resources, "workspace_dir", return_value=self.main_clone):
            free_resources(self._payload())
        assert wt.exists(), "a dirty worktree must never be removed"

    def test_ahead_of_upstream_worktree_is_skipped(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        wt = self._add_worktree("ahead", "feat-ahead")
        (wt / "b.txt").write_text("new")
        _run("git", "add", "b.txt", cwd=wt)
        _run("git", "commit", "-m", "unpushed", cwd=wt)  # ahead of upstream, not pushed
        self._make_stale(wt)
        with patch.object(mechanical_resources, "workspace_dir", return_value=self.main_clone):
            free_resources(self._payload())
        assert wt.exists(), "an ahead-of-upstream worktree must never be removed"

    def test_active_session_cwd_worktree_is_never_removed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        wt = self._add_worktree("active", "feat-active")
        self._make_stale(wt)
        with (
            patch.object(mechanical_resources, "workspace_dir", return_value=self.main_clone),
            patch.object(mechanical_resources, "_safe_cwd", return_value=wt.resolve()),
        ):
            free_resources(self._payload())
        assert wt.exists(), "the active-session worktree must never be GC'd"

    def test_gc_off_removes_nothing(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        wt = self._add_worktree("clean", "feat-clean")
        self._make_stale(wt)
        payload = self._payload()
        payload["allow_destructive_disk"] = False
        with patch.object(mechanical_resources, "workspace_dir", return_value=self.main_clone):
            free_resources(payload)
        assert wt.exists(), "with the flag off, NO worktree is removed"


class RamLadderTests(TestCase):
    """Idle-container stop runs at L2; process kill is flag + consecutive gated."""

    def test_idle_containers_are_stopped(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

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
            free_resources({"resource": "ram", "allow_destructive_ram": False})
        assert ["stop", "abc123"] in calls
        assert ["stop", "def456"] in calls
        assert ["container", "prune", "-f"] in calls

    def test_no_process_kill_when_flag_off(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_idle_containers", return_value=[]),
            patch.object(mechanical_resources, "_docker_container_prune"),
            patch.object(mechanical_resources, "os") as mock_os,
        ):
            free_resources(
                {
                    "resource": "ram",
                    "allow_destructive_ram": False,
                    "ram_kill_allowlist": ["Brave.*Renderer"],
                    "consecutive_critical": 5,
                },
            )
        mock_os.kill.assert_not_called()

    def test_no_process_kill_below_two_consecutive_ticks(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_idle_containers", return_value=[]),
            patch.object(mechanical_resources, "_docker_container_prune"),
            patch.object(mechanical_resources, "_list_processes", return_value=[(999, "Brave Renderer")]),
            patch.object(mechanical_resources, "_session_pid_ancestry", return_value=set()),
            patch.object(mechanical_resources.os, "kill") as mock_kill,
        ):
            free_resources(
                {
                    "resource": "ram",
                    "allow_destructive_ram": True,
                    "ram_kill_allowlist": ["Brave.*Renderer"],
                    "consecutive_critical": 1,
                },
            )
        mock_kill.assert_not_called()

    def test_sigterm_sent_to_allowlisted_non_session_pid(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_idle_containers", return_value=[]),
            patch.object(mechanical_resources, "_docker_container_prune"),
            patch.object(
                mechanical_resources,
                "_list_processes",
                return_value=[(999, "Brave Helper (Renderer)"), (1000, "Finder")],
            ),
            patch.object(mechanical_resources, "_session_pid_ancestry", return_value={1234}),
            patch.object(mechanical_resources.os, "kill") as mock_kill,
        ):
            free_resources(
                {
                    "resource": "ram",
                    "allow_destructive_ram": True,
                    "ram_kill_allowlist": ["Brave.*Renderer"],
                    "consecutive_critical": 2,
                },
            )
        mock_kill.assert_called_once_with(999, signal.SIGTERM)

    def test_session_ancestry_pid_is_never_killed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_idle_containers", return_value=[]),
            patch.object(mechanical_resources, "_docker_container_prune"),
            patch.object(
                mechanical_resources,
                "_list_processes",
                return_value=[(999, "Brave Helper (Renderer)")],
            ),
            patch.object(mechanical_resources, "_session_pid_ancestry", return_value={999}),
            patch.object(mechanical_resources.os, "kill") as mock_kill,
        ):
            free_resources(
                {
                    "resource": "ram",
                    "allow_destructive_ram": True,
                    "ram_kill_allowlist": ["Brave.*Renderer"],
                    "consecutive_critical": 3,
                },
            )
        mock_kill.assert_not_called()

    def test_empty_kill_allowlist_kills_nothing(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_idle_containers", return_value=[]),
            patch.object(mechanical_resources, "_docker_container_prune"),
            patch.object(mechanical_resources, "_list_processes", return_value=[(999, "Brave Renderer")]),
            patch.object(mechanical_resources, "_session_pid_ancestry", return_value=set()),
            patch.object(mechanical_resources.os, "kill") as mock_kill,
        ):
            free_resources(
                {
                    "resource": "ram",
                    "allow_destructive_ram": True,
                    "ram_kill_allowlist": [],
                    "consecutive_critical": 5,
                },
            )
        mock_kill.assert_not_called()


class ResilienceTests(TestCase):
    """A failure in any step is swallowed — the tick never crashes."""

    def test_unknown_resource_is_noop(self) -> None:
        free_resources({"resource": "nonsense"})  # must not raise

    def test_inner_exception_is_swallowed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_plan_disk", side_effect=RuntimeError("kaboom")):
            free_resources({"resource": "disk"})  # must not raise

    def test_sigterm_oserror_is_swallowed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_idle_containers", return_value=[]),
            patch.object(mechanical_resources, "_docker_container_prune"),
            patch.object(mechanical_resources, "_list_processes", return_value=[(999, "Brave Renderer")]),
            patch.object(mechanical_resources, "_session_pid_ancestry", return_value=set()),
            patch.object(mechanical_resources.os, "kill", side_effect=OSError),
        ):
            free_resources(
                {
                    "resource": "ram",
                    "allow_destructive_ram": True,
                    "ram_kill_allowlist": ["Brave.*Renderer"],
                    "consecutive_critical": 2,
                },
            )  # must not raise


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

    def test_is_within_detects_nesting(self) -> None:
        child = self.tmp / "x" / "y"
        child.mkdir(parents=True)
        assert mechanical_resources._is_within(child.resolve(), self.tmp) is True
        assert mechanical_resources._is_within(self.tmp.resolve(), child) is False

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

    def test_list_processes_parses_pid_and_name(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_ps", return_value="  101 claude\n  202 Brave Helper\nbad line\n"):
            procs = mechanical_resources._list_processes()
        assert (101, "claude") in procs
        assert (202, "Brave Helper") in procs
        assert len(procs) == 2

    def test_list_processes_none_when_ps_unavailable(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_ps", return_value=None):
            assert mechanical_resources._list_processes() == []

    def test_parent_pid_parses_ppid(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_ps", return_value=" 42\n"):
            assert mechanical_resources._parent_pid(99) == 42

    def test_parent_pid_none_on_garbage(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_ps", return_value="not-a-number\n"):
            assert mechanical_resources._parent_pid(99) is None
        with patch.object(mechanical_resources, "_ps", return_value=None):
            assert mechanical_resources._parent_pid(99) is None

    def test_session_pid_ancestry_walks_chain(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        # current pid -> 500 -> 1 (stop). Returns {getpid, 500}.
        chain = {os.getpid(): 500, 500: 1}
        with patch.object(mechanical_resources, "_parent_pid", side_effect=chain.get):
            ancestry = mechanical_resources._session_pid_ancestry()
        assert os.getpid() in ancestry
        assert 500 in ancestry

    def test_ps_returns_none_without_binary(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.shutil, "which", return_value=None):
            assert mechanical_resources._ps("-axo", "pid=") is None

    def test_docker_returns_none_without_binary(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.shutil, "which", return_value=None):
            assert mechanical_resources._docker("ps") is None

    def test_git_returns_none_without_binary(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.shutil, "which", return_value=None):
            assert mechanical_resources._git(self.tmp, "status") is None

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

    def test_safe_cwd_none_on_oserror(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.Path, "cwd", side_effect=OSError):
            assert mechanical_resources._safe_cwd() is None

    def test_persist_plan_failure_is_swallowed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        plan = mechanical_resources.FreePlan(resource="disk", steps=["x"])
        marker = ResourcePressureMarker.load()
        with patch.object(type(marker), "save", side_effect=RuntimeError("db")):
            mechanical_resources._persist_plan(marker, plan)  # must not raise

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

    def test_worktree_not_dir_is_not_eligible(self) -> None:
        assert mechanical_resources._worktree_is_gc_eligible(self.tmp / "absent", stale_days=30) is False

    def test_git_dirty_treats_none_as_dirty(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_git", return_value=None):
            assert mechanical_resources._git_dirty(self.tmp) is True

    def test_git_ahead_treats_none_as_ahead(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_git", return_value=None):
            assert mechanical_resources._git_ahead_of_upstream(self.tmp) is True

    def test_is_stale_false_when_stat_fails(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.Path, "stat", side_effect=OSError):
            assert mechanical_resources._is_stale(self.tmp, stale_days=30) is False

    def test_list_worktrees_empty_when_workspace_missing(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "workspace_dir", return_value=self.tmp / "absent"):
            assert mechanical_resources._list_workspace_worktrees() == []

    def test_list_worktrees_empty_when_git_unavailable(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "workspace_dir", return_value=self.tmp),
            patch.object(mechanical_resources, "_git", return_value=None),
        ):
            assert mechanical_resources._list_workspace_worktrees() == []

    def test_idle_containers_empty_when_docker_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_docker", return_value=None):
            assert mechanical_resources._idle_containers() == []

    def test_session_ancestry_stops_when_parent_unknown(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources, "_parent_pid", return_value=None):
            ancestry = mechanical_resources._session_pid_ancestry()
        assert ancestry == {os.getpid()}

    def test_kill_candidates_empty_with_no_patterns(self) -> None:
        assert mechanical_resources._kill_candidate_pids({"ram_kill_allowlist": []}) == []

    def test_is_within_false_on_resolve_error(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.Path, "resolve", side_effect=OSError):
            assert mechanical_resources._is_within(self.tmp, self.tmp) is False

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

    def test_gc_candidates_respects_per_tick_cap(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        worktrees = [self.tmp / f"wt{i}" for i in range(5)]
        for wt in worktrees:
            wt.mkdir()
        with (
            patch.object(mechanical_resources, "_list_workspace_worktrees", return_value=worktrees),
            patch.object(mechanical_resources, "_safe_cwd", return_value=None),
            patch.object(mechanical_resources, "_worktree_is_gc_eligible", return_value=True),
        ):
            candidates = mechanical_resources._gc_candidate_worktrees(
                {"worktree_stale_days": 30, "max_worktree_gc_per_tick": 2},
            )
        assert len(candidates) == 2

    def test_docker_invokes_run_when_binary_present(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources.shutil, "which", return_value="/usr/bin/docker"),
            patch.object(mechanical_resources, "_run", return_value="abc\n") as mock_run,
        ):
            assert mechanical_resources._docker("ps") == "abc\n"
        mock_run.assert_called_once()

    def test_sigterm_logs_on_success(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(mechanical_resources.os, "kill") as mock_kill:
            mechanical_resources._sigterm(4242)
        mock_kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_ps_invokes_run_when_binary_present(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources.shutil, "which", return_value="/bin/ps"),
            patch.object(mechanical_resources, "_run", return_value="101 claude\n") as mock_run,
        ):
            assert mechanical_resources._ps("-axo", "pid=,comm=") == "101 claude\n"
        mock_run.assert_called_once()

    def test_gc_worktrees_skips_size_when_remove_fails(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch.object(mechanical_resources, "_gc_candidate_worktrees", return_value=[str(self.tmp / "wt")]),
            patch.object(mechanical_resources, "_dir_size_gb", return_value=2.0),
            patch.object(mechanical_resources, "_remove_worktree", return_value=False),
        ):
            reclaimed = mechanical_resources._gc_worktrees({})
        assert reclaimed == pytest.approx(0.0), "a failed removal must not count toward reclaimed bytes"


def _fake_reclaim_report(total_bytes: int = 0) -> object:
    """A stand-in ``ReclaimReport`` whose ``total_bytes`` the ladder reads."""
    from teatree.docker.reclaim import PruneOutcome, ReclaimReport, ReclaimStep  # noqa: PLC0415

    step = ReclaimStep(
        argv=["docker", "builder", "prune", "-af"],
        label="build cache",
        outcome=PruneOutcome(reclaimed="x", bytes_reclaimed=total_bytes),
    )
    return ReclaimReport(steps=(step,), planned=(step,), dry_run=False)


def _rmtree_safe(path: str) -> None:
    import shutil  # noqa: PLC0415

    shutil.rmtree(path, ignore_errors=True)
