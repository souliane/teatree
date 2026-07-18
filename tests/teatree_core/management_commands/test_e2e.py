"""Tests for the e2e management command."""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.config as config_mod
import teatree.core.backend_factory as backend_factory_mod
import teatree.core.management.commands._e2e_discovery as e2e_disc_mod
import teatree.core.management.commands._e2e_runners as e2e_runners_mod
import teatree.core.management.commands.e2e as e2e_mod
import teatree.utils.run as utils_run_mod
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.management_commands._overlays import (
    _EXTERNAL_RUNNER_OVERLAY,
    _INFER_EXTERNAL_OVERLAY,
    _INFER_PROJECT_OVERLAY,
    _OVERLAY_REPO_OVERLAY,
    _PLAYWRIGHT_ARGS_OVERLAY,
    _PROJECT_RUNNER_OVERLAY,
    _UNCONFIGURED_OVERLAY,
    FULL_OVERLAY,
    MINIMAL_OVERLAY,
    PROVENANCE_OVERLAY,
    SETTINGS,
    FullE2E,
    _patch_overlays,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_GIT = shutil.which("git") or "git"


def _seed_config(db: Path, key: str, value: object, scope: str = "") -> None:
    """Seed a ``teatree_config_setting`` row the cold reader resolves."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            (scope, key, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


def _popen_for(result: MagicMock) -> MagicMock:
    """Wrap a ``MagicMock(returncode=N)`` into a ``Popen`` context-manager mock.

    ``run_streamed`` now drives ``Popen``: it tees ``proc.stderr`` then reads
    ``proc.wait()``. The returned mock records the ``Popen(cmd, env=...)`` call
    (so ``call_args[0][0]`` / ``call_args[1]["env"]`` assertions keep working)
    and yields a proc whose ``wait()`` returns the original mock's returncode.
    """
    proc = MagicMock()
    proc.stderr = iter(getattr(result, "_streamed_stderr", ()) or ())
    proc.wait.return_value = result.returncode
    ctx = MagicMock()
    ctx.__enter__.return_value = proc
    ctx.__exit__.return_value = False
    return MagicMock(return_value=ctx)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run([_GIT, "-C", str(cwd), *args], capture_output=True, text=True, check=True)


def _make_upstream_with_branches(base: Path, branches: tuple[str, ...]) -> Path:
    """Create a real upstream repo carrying *branches*; return its path (the clone URL)."""
    upstream = base / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "Test")
    (upstream / "e2e").mkdir()
    (upstream / "e2e" / "playwright.config.ts").write_text("export default {};\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "init")
    for branch in branches:
        _git(upstream, "branch", branch)
    return upstream


class TestE2eTriggerCi(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_triggers_pipeline(self) -> None:
        mock_ci = MagicMock()
        mock_ci.trigger_pipeline.return_value = {"pipeline_id": 123}

        with patch.object(backend_factory_mod, "ci_service_from_overlay", return_value=mock_ci):
            result = cast("dict[str, object]", call_command("e2e", "trigger-ci"))

        assert result == {"pipeline_id": 123}
        mock_ci.trigger_pipeline.assert_called_once_with(
            project="test/e2e-project",
            ref="main",
            variables={"E2E": "true"},
        )

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_branch_override(self) -> None:
        mock_ci = MagicMock()
        mock_ci.trigger_pipeline.return_value = {"pipeline_id": 456}

        with patch.object(backend_factory_mod, "ci_service_from_overlay", return_value=mock_ci):
            cast("dict[str, object]", call_command("e2e", "trigger-ci", branch="feature-branch"))

        mock_ci.trigger_pipeline.assert_called_once_with(
            project="test/e2e-project",
            ref="feature-branch",
            variables={"E2E": "true"},
        )

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_config_returns_error(self) -> None:
        result = cast("dict[str, object]", call_command("e2e", "trigger-ci"))

        assert "error" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_ci_service_returns_error(self) -> None:
        with patch.object(backend_factory_mod, "ci_service_from_overlay", return_value=None):
            result = cast("dict[str, object]", call_command("e2e", "trigger-ci"))

        assert "error" in result


class TestE2eProject(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_playwright_locally(self) -> None:
        mock_result = MagicMock(returncode=0)
        with (
            patch.object(e2e_mod, "resolve_worktree", return_value=None),
            patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
        ):
            result = cast("str", call_command("e2e", "project", docker=False))

        assert "passed" in result
        cmd = mock_run.call_args[0][0]
        assert "pytest" in cmd
        assert "e2e/" in cmd

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_reports_failure(self) -> None:
        mock_result = MagicMock(returncode=1)
        with (
            patch.object(e2e_mod, "resolve_worktree", return_value=None),
            patch.object(utils_run_mod, "Popen", _popen_for(mock_result)),
            pytest.raises(SystemExit) as exc_info,
        ):
            call_command("e2e", "project", docker=False)

        assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_headed_mode_skips_ci_env(self) -> None:
        """--headed does not set CI=1 in the environment."""
        mock_result = MagicMock(returncode=0)
        with (
            patch.object(e2e_mod, "resolve_worktree", return_value=None),
            patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
        ):
            call_command("e2e", "project", headed=True, docker=False)

        env = mock_run.call_args[1].get("env", {})
        assert env.get("CI") != "1"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_custom_test_path(self) -> None:
        """e2e project uses the specified test path instead of e2e/."""
        mock_result = MagicMock(returncode=0)
        with (
            patch.object(e2e_mod, "resolve_worktree", return_value=None),
            patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
        ):
            call_command("e2e", "project", test_path="tests/e2e/test_login.py", docker=False)

        cmd = mock_run.call_args[0][0]
        assert "tests/e2e/test_login.py" in cmd
        assert "e2e/" not in cmd

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_docker_passes_test_path_and_update_snapshots(self) -> None:
        """Docker path forwards --test-path and --update-snapshots to the e2e service."""
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            (wt / "dev").mkdir()
            (wt / "dev" / "docker-compose.yml").touch()

            mock_result = MagicMock(returncode=0)
            real_exists = Path.exists

            def fake_exists(self: Path) -> bool:
                # Pretend the in-Docker marker is absent so the command takes the
                # docker-compose branch even when the suite itself runs in Docker.
                if str(self) == "/.dockerenv":
                    return False
                return real_exists(self)

            with (
                patch.object(
                    e2e_mod,
                    "resolve_worktree",
                    return_value=MagicMock(extra={"worktree_path": str(wt)}),
                ),
                patch.object(Path, "exists", fake_exists),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                call_command(
                    "e2e",
                    "project",
                    test_path="e2e/test_smoke.py::test_smoke",
                    update_snapshots=True,
                )

            cmd = mock_run.call_args[0][0]
            assert "docker" in cmd
            assert "e2e/test_smoke.py::test_smoke" in cmd
            assert "--update-snapshots" in cmd


class TestE2eRunWorkItem(TestCase):
    """``t3 <ov> e2e run <work-item>`` — #794 single-command MVP.

    Resolves the work item by its Ticket natural key, applies the default
    environment ladder, runs the existing workspace as-is or emits a precise
    readiness failure naming the exact provisioning gap, and records run
    provenance on the durable recipe.
    """

    def _git(self, path: Path, *args: str) -> None:
        subprocess.run([_GIT, "-C", str(path), *args], check=True, capture_output=True, text=True)

    def _make_repo(self, path: Path) -> str:
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init", "-q", "-b", "main")
        self._git(path, "config", "user.email", "t@t.test")
        self._git(path, "config", "user.name", "T")
        (path / "f").write_text("x")
        self._git(path, "add", "-A")
        self._git(path, "commit", "-q", "-m", "c0")
        return subprocess.run(
            [_GIT, "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_existing_workspace_runs_and_records_green_provenance(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(str(d), ignore_errors=True))
        wt_dir = d / "backend"
        sha = self._make_repo(wt_dir)
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/o/r/issues/794")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feat",
            extra={"worktree_path": str(wt_dir)},
        )

        with patch.object(e2e_mod.Command, "_dispatch_runner", return_value="E2E passed.") as run_existing:
            result = cast("str", call_command("e2e", "run", "794"))

        assert "passed" in result.lower()
        run_existing.assert_called_once()
        from teatree.core.intake.e2e_workitem import load_recipe  # noqa: PLC0415

        recipe = load_recipe(Ticket.objects.get(pk=ticket.pk))
        assert recipe.last_run is not None
        assert recipe.last_run["result"] == "green"
        assert recipe.last_run["per_repo_shas"] == {"backend": sha}

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_workspace_emits_precise_readiness_failure_naming_the_ref(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/o/r/issues/55")
        ticket.repos = ["backend"]
        ticket.save(update_fields=["repos"])

        with pytest.raises(SystemExit) as exc:
            call_command("e2e", "run", "55")

        assert exc.value.code != 0

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_unknown_work_item_fails_clearly(self) -> None:
        with pytest.raises(SystemExit) as exc:
            call_command("e2e", "run", "https://github.com/o/r/issues/99999")

        assert exc.value.code != 0

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failed_run_records_red_provenance_then_re_raises(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(str(d), ignore_errors=True))
        wt_dir = d / "backend"
        sha = self._make_repo(wt_dir)
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/o/r/issues/77")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feat",
            extra={"worktree_path": str(wt_dir)},
        )

        with (
            patch.object(e2e_mod.Command, "_dispatch_runner", side_effect=SystemExit(3)),
            pytest.raises(SystemExit) as exc,
        ):
            call_command("e2e", "run", "77")

        assert exc.value.code == 3
        from teatree.core.intake.e2e_workitem import load_recipe  # noqa: PLC0415

        recipe = load_recipe(Ticket.objects.get(pk=ticket.pk))
        assert recipe.last_run is not None
        assert recipe.last_run["result"] == "red"
        assert recipe.last_run["per_repo_shas"] == {"backend": sha}

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_unreadable_repo_sha_is_recorded_empty_not_crash(self) -> None:
        # The worktree dir exists (reconcile says "existing") but is not a
        # git repo → head_sha raises; provenance records "" not a crash.
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(str(d), ignore_errors=True))
        wt_dir = d / "backend"
        wt_dir.mkdir(parents=True)
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/o/r/issues/88")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feat",
            extra={"worktree_path": str(wt_dir)},
        )

        with patch.object(e2e_mod.Command, "_dispatch_runner", return_value="E2E passed."):
            call_command("e2e", "run", "88")

        from teatree.core.intake.e2e_workitem import load_recipe  # noqa: PLC0415

        recipe = load_recipe(Ticket.objects.get(pk=ticket.pk))
        assert recipe.last_run is not None
        assert recipe.last_run["per_repo_shas"] == {"backend": ""}

    def _provision_workspace(self, issue: str) -> Ticket:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(str(d), ignore_errors=True))
        wt_dir = d / "backend"
        self._make_repo(wt_dir)
        ticket = Ticket.objects.create(overlay="test", issue_url=f"https://github.com/o/r/issues/{issue}")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feat",
            extra={"worktree_path": str(wt_dir)},
        )
        return ticket

    @_patch_overlays(PROVENANCE_OVERLAY)
    @override_settings(**SETTINGS)
    def test_green_run_records_spec_path_and_overlay_resolved_manifest_entry(self) -> None:
        ticket = self._provision_workspace("272")
        spec = "e2e/specs/tenant-a/workflow/feature-x.spec.ts"

        with patch.object(e2e_mod.Command, "_dispatch_runner", return_value="E2E passed."):
            call_command("e2e", "run", "272", test_path=spec)

        from teatree.core.intake.e2e_workitem import load_recipe  # noqa: PLC0415

        recipe = load_recipe(Ticket.objects.get(pk=ticket.pk))
        assert recipe.last_run is not None
        assert recipe.last_run["result"] == "green"
        assert recipe.last_run["spec_path"] == spec
        assert recipe.last_run["manifest_entry"] == "feature-x-lane"

    @_patch_overlays(PROVENANCE_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failed_run_also_records_spec_provenance(self) -> None:
        ticket = self._provision_workspace("273")
        spec = "e2e/specs/tenant-b/workflow/feature-y.spec.ts"

        with (
            patch.object(e2e_mod.Command, "_dispatch_runner", side_effect=SystemExit(3)),
            pytest.raises(SystemExit),
        ):
            call_command("e2e", "run", "273", test_path=spec)

        from teatree.core.intake.e2e_workitem import load_recipe  # noqa: PLC0415

        recipe = load_recipe(Ticket.objects.get(pk=ticket.pk))
        assert recipe.last_run is not None
        assert recipe.last_run["result"] == "red"
        assert recipe.last_run["spec_path"] == spec
        assert recipe.last_run["manifest_entry"] == "feature-y-lane"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_overlay_without_manifest_records_no_manifest_entry(self) -> None:
        ticket = self._provision_workspace("274")
        spec = "e2e/specs/misc/smoke.spec.ts"

        with patch.object(e2e_mod.Command, "_dispatch_runner", return_value="E2E passed."):
            call_command("e2e", "run", "274", test_path=spec)

        from teatree.core.intake.e2e_workitem import load_recipe  # noqa: PLC0415

        recipe = load_recipe(Ticket.objects.get(pk=ticket.pk))
        assert recipe.last_run is not None
        assert recipe.last_run["spec_path"] == spec
        assert "manifest_entry" not in recipe.last_run


class TestE2eExternal(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_private_tests_configured_raises_system_exit_1(self) -> None:
        """Unconfigured private_tests is a misconfig that must stop the caller.

        Regression for #932: returning the message exited 0, so an
        unconfigured E2E external run looked green.
        """
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("T3_PRIVATE_TESTS", None)
            os.environ.pop("T3_CONFIG_DB", None)  # no DB private_tests row → unconfigured
            with pytest.raises(SystemExit) as exc_info:
                call_command("e2e", "external")
        assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_target_dev_without_base_url_raises_system_exit_1(self) -> None:
        """`e2e external --target dev` without BASE_URL is a misconfig — exit 1.

        Regression for #932: the message was written to stderr but the
        command still returned (exit 0).
        """
        with tempfile.TemporaryDirectory() as tmp:
            private_dir = Path(tmp) / "private"
            private_dir.mkdir()
            with (
                patch.dict("os.environ", {"T3_PRIVATE_TESTS": str(private_dir)}, clear=False),
                patch.object(e2e_mod.Command, "_resolve_target", return_value="dev"),
            ):
                os.environ.pop("BASE_URL", None)
                with pytest.raises(SystemExit) as exc_info:
                    call_command("e2e", "external", target="dev")
            assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_db_config_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()
            private_dir = tmp_path / "private"
            private_dir.mkdir()
            db = tmp_path / "db.sqlite3"
            _seed_config(db, "private_tests", str(private_dir))

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/cfg")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )

            mock_result = MagicMock(returncode=0)
            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": str(wt_dir), "T3_CONFIG_DB": str(db)}, clear=False),
                patch.object(e2e_disc_mod, "get_service_port", return_value=4200),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)),
            ):
                os.environ.pop("T3_PRIVATE_TESTS", None)
                result = cast("str", call_command("e2e", "external"))
            assert "passed" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_private_tests_dir_missing_raises_system_exit_1(self) -> None:
        """A missing private_tests directory is a misconfig — exit 1."""
        with (
            patch.dict("os.environ", {"T3_PRIVATE_TESTS": "/nonexistent/path"}),
            pytest.raises(SystemExit) as exc_info,
        ):
            call_command("e2e", "external")
        assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_external_tests_with_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()
            # The env cache lives out-of-repo under the ticket dir's .t3-cache/
            # sibling, per repo, never inside the repo working tree
            # (souliane/teatree#3097).
            cache_dir = wt_dir.parent / ".t3-cache" / wt_dir.name
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / ".t3-env.cache").write_text(f"WT_VARIANT=acme\nTICKET_DIR={tmp_path}\n", encoding="utf-8")
            private_dir = tmp_path / "private"
            private_dir.mkdir()

            ticket = Ticket.objects.create(
                overlay="test",
                issue_url="https://example.com/issues/variant",
                variant="acme",
            )
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )
            mock_result = MagicMock(returncode=0)
            with (
                patch.dict("os.environ", {"T3_PRIVATE_TESTS": str(private_dir), "T3_ORIG_CWD": str(wt_dir)}),
                patch.object(e2e_disc_mod, "get_service_port", return_value=5555),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                result = cast("str", call_command("e2e", "external"))
            assert "passed" in result
            env = mock_run.call_args[1]["env"]
            assert env["BASE_URL"] == "http://localhost:5555"
            assert env["CUSTOMER"] == "acme"
            assert env["CI"] == "1"

    @_patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY)
    @override_settings(**SETTINGS)
    def test_overlay_playwright_args_select_config_per_spec(self) -> None:
        """An overlay maps a spec lane to its Playwright config (``-c <config>``).

        A multi-config Playwright suite needs the right ``-c`` per lane; the
        overlay knows the lane->config mapping. The external runner must thread
        ``e2e.playwright_args(spec)`` into the ``npx playwright test``
        command — without it, an api-flow spec runs under the wrong default
        config (the UI-login one) and fails.
        """
        spec = "playwright/api-flow/agent-portal/tests/api-tests/acme.spec.ts"
        with self._external_run(spec) as mock_run:
            cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["npx", "playwright", "test"]
        assert "-c" in cmd
        assert cmd[cmd.index("-c") + 1] == "api.config.ts"
        assert spec in cmd

    @_patch_overlays(_PLAYWRIGHT_ARGS_OVERLAY)
    @override_settings(**SETTINGS)
    def test_overlay_playwright_args_empty_for_other_lanes(self) -> None:
        """A spec the overlay does not map adds no Playwright args (default-empty)."""
        spec = "playwright/contrib/acme/loan-request/child-allowance.spec.ts"
        with self._external_run(spec) as mock_run:
            cmd = mock_run.call_args[0][0]
        assert "-c" not in cmd
        assert spec in cmd

    @contextmanager
    def _external_run(self, spec: str) -> Iterator[MagicMock]:
        """Run ``e2e external <spec> --target local`` against a provisioned worktree.

        Yields the ``Popen`` mock so the caller can assert the built command.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()
            private_dir = tmp_path / "private"
            private_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/pwargs")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )
            mock_result = MagicMock(returncode=0)
            with (
                patch.dict("os.environ", {"T3_PRIVATE_TESTS": str(private_dir), "T3_ORIG_CWD": str(wt_dir)}),
                patch.object(e2e_disc_mod, "get_service_port", return_value=5555),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                call_command("e2e", "external", test_path=spec, target="local")
                yield mock_run

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_local_target_exports_compose_project_name(self) -> None:
        """The local target hands the teatree compose project to the spec.

        A spec that resolves the backend via a bare ``docker compose port
        web 8000`` / ``docker compose exec -T web`` (run from the backend
        repo dir, no ``-p``) would otherwise default the project name to
        the directory basename and miss the teatree-managed stack — the one
        whose ``web`` container has the restored-Postgres ``DATABASE_URL``
        injected. Exporting ``COMPOSE_PROJECT_NAME`` (the value
        ``compose_project(worktree)`` returns) makes those bare
        ``docker compose`` calls deterministically target the provisioned
        stack with no spec change.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()
            private_dir = tmp_path / "private"
            private_dir.mkdir()

            ticket = Ticket.objects.create(
                overlay="test",
                issue_url="https://example.com/issues/1151",
            )
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )
            mock_result = MagicMock(returncode=0)
            with (
                patch.dict("os.environ", {"T3_PRIVATE_TESTS": str(private_dir), "T3_ORIG_CWD": str(wt_dir)}),
                patch.object(e2e_disc_mod, "get_service_port", return_value=5555),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                result = cast("str", call_command("e2e", "external", target="local"))
            assert "passed" in result
            env = mock_run.call_args[1]["env"]
            assert env["COMPOSE_PROJECT_NAME"] == f"backend-wt{ticket.pk}"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_linked_to_routes_discovery_at_named_ticket(self) -> None:
        """``--linked-to <ticket>`` ties the e2e cache repo's run to a backend ticket.

        Defect 1 of souliane/teatree#1322: when the external e2e runner runs
        from an external e2e cache repo whose auto-registered worktree is
        ticketless or belongs to a different ticket than the backend stack,
        frontend port discovery returned None and the run aborted. The
        explicit link tells the runner which backend ticket owns the stack
        and the COMPOSE_PROJECT_NAME / env-cache lookup also routes through
        that ticket — so a single command boots the spec against the
        linked backend without manual ``BASE_URL``/``COMPOSE_PROJECT_NAME``
        overrides.
        """
        with tempfile.TemporaryDirectory() as tmp:
            captured_env_caches: list[dict[str, str]] = []

            def _e2e_env_extras(self: object, env_cache: dict[str, str], **_kwargs: object) -> dict[str, str]:
                _ = self
                captured_env_caches.append(dict(env_cache))
                return {
                    "CUSTOMER": env_cache.get("WT_VARIANT", ""),
                    "SPEC_PATH_SEEN": env_cache.get("T3_E2E_TEST_PATH", ""),
                }

            tmp_path = Path(tmp)
            backend_wt_dir = tmp_path / "backend-worktree"
            backend_wt_dir.mkdir()
            # The env cache lives in the linked backend ticket's out-of-repo
            # .t3-cache/ sibling, per repo, never inside the repo working tree
            # (souliane/teatree#3097) — overlay extras (CUSTOMER, app
            # credentials) are sourced from there.
            cache_dir = backend_wt_dir.parent / ".t3-cache" / backend_wt_dir.name
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / ".t3-env.cache").write_text(
                f"WT_VARIANT=tenant-child\nTICKET_DIR={backend_wt_dir.parent}\n",
                encoding="utf-8",
            )
            e2e_cache_dir = tmp_path / "e2e-cache"
            e2e_cache_dir.mkdir()
            private_dir = tmp_path / "private"
            private_dir.mkdir()

            # Backend ticket: the stack the user wants to test against.
            backend_ticket = Ticket.objects.create(
                overlay="test",
                issue_url="https://example.com/issues/backend",
                variant="tenant-child",
            )
            Worktree.objects.create(
                overlay="test",
                ticket=backend_ticket,
                repo_path="backend-repo",
                branch="backend",
                extra={"worktree_path": str(backend_wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )

            # E2E cache "worktree": auto-registered, ticketless or different
            # ticket. The user's CWD when calling `e2e external` is the cache.
            e2e_ticket = Ticket.objects.create(
                overlay="test",
                issue_url="auto:e2e-cache",
            )
            Worktree.objects.create(
                overlay="test",
                ticket=e2e_ticket,
                repo_path="e2e-cache-repo",
                branch="e2e",
                extra={"worktree_path": str(e2e_cache_dir)},
            )

            mock_result = MagicMock(returncode=0)
            with (
                patch.dict(
                    "os.environ",
                    {"T3_PRIVATE_TESTS": str(private_dir), "T3_ORIG_CWD": str(e2e_cache_dir)},
                    clear=False,
                ),
                patch.object(e2e_disc_mod, "get_service_port", return_value=62674),
                patch.object(FullE2E, "env_extras", _e2e_env_extras),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                os.environ.pop("BASE_URL", None)
                result = cast(
                    "str",
                    call_command(
                        "e2e",
                        "external",
                        test_path="tests/specs/loan-flow.spec.ts",
                        target="local",
                        linked_to=backend_ticket.pk,
                    ),
                )

            assert "passed" in result
            env = mock_run.call_args[1]["env"]
            assert captured_env_caches == [
                {
                    "WT_VARIANT": "tenant-child",
                    "TICKET_DIR": str(backend_wt_dir.parent),
                    "T3_E2E_TEST_PATH": "tests/specs/loan-flow.spec.ts",
                },
            ]
            # Frontend discovered via the linked backend worktree's project.
            assert env["BASE_URL"] == "http://localhost:62674"
            # COMPOSE_PROJECT_NAME points at the backend worktree's project,
            # not the e2e cache worktree's.
            assert env["COMPOSE_PROJECT_NAME"] == f"backend-repo-wt{backend_ticket.pk}"
            # Defect 2: the env-cache that feeds e2e.env_extras must be
            # the linked backend worktree's, so overlay-derived extras (e.g.
            # CUSTOMER=<variant>) reach the spec.
            assert env["CUSTOMER"] == "tenant-child"
            assert env["SPEC_PATH_SEEN"] == "tests/specs/loan-flow.spec.ts"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_linked_to_resolves_without_cwd_worktree_row(self) -> None:
        """``--linked-to`` succeeds even when cwd has no worktree row.

        Regression for souliane/teatree#2287: ``_resolve_target_env`` called
        ``resolve_worktree()`` unconditionally before the link routing, so
        running from a standalone external e2e repo (no ``.t3-cache`` env
        cache, no DB row for the cwd) raised ``WorktreeNotFoundError`` before
        ``--linked-to`` could supply the backend worktree.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            backend_wt_dir = tmp_path / "backend-worktree"
            backend_wt_dir.mkdir()
            # The env cache lives out-of-repo under the ticket dir's .t3-cache/
            # sibling, per repo, never inside the repo working tree
            # (souliane/teatree#3097).
            cache_dir = backend_wt_dir.parent / ".t3-cache" / backend_wt_dir.name
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / ".t3-env.cache").write_text(
                f"WT_VARIANT=acme\nTICKET_DIR={backend_wt_dir.parent}\n",
                encoding="utf-8",
            )
            standalone_e2e_dir = tmp_path / "standalone-e2e"
            standalone_e2e_dir.mkdir()
            private_dir = tmp_path / "private"
            private_dir.mkdir()

            backend_ticket = Ticket.objects.create(
                overlay="test",
                issue_url="https://example.com/issues/2287",
                variant="acme",
            )
            Worktree.objects.create(
                overlay="test",
                ticket=backend_ticket,
                repo_path="backend-repo",
                branch="backend",
                extra={"worktree_path": str(backend_wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )

            mock_result = MagicMock(returncode=0)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "T3_PRIVATE_TESTS": str(private_dir),
                        "T3_ORIG_CWD": str(standalone_e2e_dir),
                    },
                    clear=False,
                ),
                patch.object(e2e_disc_mod, "get_service_port", return_value=4202),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                os.environ.pop("BASE_URL", None)
                result = cast(
                    "str",
                    call_command("e2e", "external", target="local", linked_to=backend_ticket.pk),
                )

            assert "passed" in result
            env = mock_run.call_args[1]["env"]
            assert env["BASE_URL"] == "http://localhost:4202"
            assert env["COMPOSE_PROJECT_NAME"] == f"backend-repo-wt{backend_ticket.pk}"
            assert env["CUSTOMER"] == "acme"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_linked_to_unknown_ticket_exits_with_error(self) -> None:
        """``--linked-to <bogus-pk>`` is a misconfig — fail fast with exit 2.

        Misconfigured link IDs must not silently fall through to the
        resolved-worktree path; that would mask the user's intent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            private_dir = Path(tmp) / "private"
            private_dir.mkdir()

            with (
                patch.dict("os.environ", {"T3_PRIVATE_TESTS": str(private_dir)}, clear=False),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("e2e", "external", target="local", linked_to=9_999_999)

            assert exc_info.value.code == 2

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_remote_targets_do_not_export_compose_project_name(self) -> None:
        """DEV/QA targets hit deployed envs — no local stack to point at.

        ``COMPOSE_PROJECT_NAME`` must not leak into a dev run (no local
        docker stack exists; a stray value would mis-scope any incidental
        ``docker compose`` call the spec makes on dev).
        """
        for target, base_url in [
            ("dev", "https://dev.example.com"),
            ("qa", "https://qa.example.com"),
        ]:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                private_dir = Path(tmp) / "private"
                private_dir.mkdir()

                mock_result = MagicMock(returncode=0)
                with (
                    patch.dict(
                        "os.environ",
                        {"T3_PRIVATE_TESTS": str(private_dir), "BASE_URL": base_url},
                        clear=False,
                    ),
                    patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
                ):
                    os.environ.pop("COMPOSE_PROJECT_NAME", None)
                    result = cast("str", call_command("e2e", "external", target=target))

                assert "passed" in result
                env = mock_run.call_args[1]["env"]
                assert env["BASE_URL"] == base_url
                assert env["T3_E2E_TARGET"] == target
                assert "COMPOSE_PROJECT_NAME" not in env

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_headed_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()
            private_dir = tmp_path / "private"
            private_dir.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/headed")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )
            mock_result = MagicMock(returncode=1)
            with (
                patch.dict("os.environ", {"T3_PRIVATE_TESTS": str(private_dir), "T3_ORIG_CWD": str(wt_dir)}),
                patch.object(e2e_disc_mod, "get_service_port", return_value=4200),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("e2e", "external", headed=True)
            assert exc_info.value.code == 1
            cmd = mock_run.call_args[0][0]
            assert "--headed" in cmd
            env = mock_run.call_args[1]["env"]
            assert "CI" not in env

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_custom_test_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            private_dir = tmp_path / "private"
            private_dir.mkdir()
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/path")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )
            mock_result = MagicMock(returncode=0)
            with (
                patch.dict("os.environ", {"T3_PRIVATE_TESTS": str(private_dir), "T3_ORIG_CWD": str(wt_dir)}),
                patch.object(e2e_disc_mod, "get_service_port", return_value=4200),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                call_command("e2e", "external", test_path="tests/login.py")
            cmd = mock_run.call_args[0][0]
            assert "tests/login.py" in cmd

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_frontend_not_running_raises_system_exit_1(self) -> None:
        """A missing local frontend is an unmet precondition — exit 1 (#932)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            private_dir = tmp_path / "private"
            private_dir.mkdir()
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/nofe")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )
            with (
                patch.dict("os.environ", {"T3_PRIVATE_TESTS": str(private_dir), "T3_ORIG_CWD": str(wt_dir)}),
                patch.object(e2e_disc_mod, "get_service_port", return_value=None),
                patch.object(e2e_mod, "_detect_local_port", return_value=None),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("e2e", "external")
            assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_base_url_env_skips_port_discovery(self) -> None:
        """When BASE_URL is set, port discovery is skipped and BASE_URL is preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            private_dir = Path(tmp) / "private"
            private_dir.mkdir()

            mock_result = MagicMock(returncode=0)
            with (
                patch.dict(
                    "os.environ",
                    {"T3_PRIVATE_TESTS": str(private_dir), "BASE_URL": "https://dev.example.com"},
                    clear=False,
                ),
                patch.object(e2e_mod, "_discover_frontend_port") as mock_discover,
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                result = cast("str", call_command("e2e", "external"))

        assert "passed" in result
        mock_discover.assert_not_called()
        env = mock_run.call_args[1]["env"]
        assert env["BASE_URL"] == "https://dev.example.com"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_base_url_env_with_repo_flag(self) -> None:
        """BASE_URL + --repo: clones repo and skips port discovery."""
        with tempfile.TemporaryDirectory() as tmp:
            playwright_root = Path(tmp) / "clone" / "e2e"
            playwright_root.mkdir(parents=True)

            repo = config_mod.E2ERepo(name="svc", url="git@example.com:org/svc.git", branch="main")
            mock_result = MagicMock(returncode=0)
            with (
                patch.dict("os.environ", {"BASE_URL": "https://dev.example.com"}, clear=False),
                patch.object(e2e_runners_mod, "load_e2e_repos", return_value=[repo]),
                patch.object(e2e_runners_mod, "clone_or_update_e2e_repo", return_value=playwright_root),
                patch.object(e2e_mod, "_discover_frontend_port") as mock_discover,
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                result = cast("str", call_command("e2e", "external", repo="svc"))

        assert "passed" in result
        mock_discover.assert_not_called()
        env = mock_run.call_args[1]["env"]
        assert env["BASE_URL"] == "https://dev.example.com"


class TestE2eExternalPreflight(TestCase):
    """``e2e external`` invokes ``overlay.e2e.preflight()`` before launching Playwright."""

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_passes_customer_and_base_url_then_runs_playwright(self) -> None:
        recorded: list[dict[str, str | None]] = []

        def _record(self_overlay: object, *, customer: str | None, base_url: str | None) -> list[Callable[[], None]]:
            _ = self_overlay
            recorded.append({"customer": customer, "base_url": base_url})
            return [lambda: None]

        with tempfile.TemporaryDirectory() as tmp:
            private_dir = Path(tmp) / "private"
            private_dir.mkdir()
            mock_result = MagicMock(returncode=0)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "T3_PRIVATE_TESTS": str(private_dir),
                        "BASE_URL": "https://dev.example.com",
                        "CUSTOMER": "acme",
                    },
                    clear=False,
                ),
                patch.object(FullE2E, "preflight", new=_record),
                patch.object(e2e_mod, "_discover_frontend_port"),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)),
            ):
                result = cast("str", call_command("e2e", "external"))

        assert "passed" in result
        assert recorded == [{"customer": "acme", "base_url": "https://dev.example.com"}]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failing_check_aborts_before_playwright(self) -> None:
        def _failing(self_overlay: object, *, customer: str | None, base_url: str | None) -> list[Callable[[], None]]:
            _ = self_overlay, customer, base_url

            def _fail() -> None:
                msg = "Vendor SSO rejected stored credentials"
                raise RuntimeError(msg)

            return [_fail]

        with tempfile.TemporaryDirectory() as tmp:
            private_dir = Path(tmp) / "private"
            private_dir.mkdir()
            mock_result = MagicMock(returncode=0)
            with (
                patch.dict(
                    "os.environ",
                    {
                        "T3_PRIVATE_TESTS": str(private_dir),
                        "BASE_URL": "https://dev.example.com",
                    },
                    clear=False,
                ),
                patch.object(FullE2E, "preflight", new=_failing),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
                patch.object(e2e_mod, "_discover_frontend_port"),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("e2e", "external")

        assert exc_info.value.code != 0
        mock_run.assert_not_called()


class TestE2eRun(TestCase):
    """`t3 <overlay> e2e run` dispatches to project/external based on overlay config."""

    @_patch_overlays(_PROJECT_RUNNER_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runner_project_invokes_pytest(self) -> None:
        mock_result = MagicMock(returncode=0)
        with (
            patch.object(e2e_mod, "resolve_worktree", return_value=None),
            patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
        ):
            result = cast("str", call_command("e2e", "run", docker=False))

        assert "passed" in result
        cmd = mock_run.call_args[0][0]
        assert "pytest" in cmd

    @_patch_overlays(_EXTERNAL_RUNNER_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runner_external_invokes_playwright(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"T3_PRIVATE_TESTS": tmp, "BASE_URL": "https://dev.example"}, clear=False),
        ):
            mock_result = MagicMock(returncode=0)
            with patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run:
                result = cast("str", call_command("e2e", "run"))

            assert "passed" in result
            cmd = mock_run.call_args[0][0]
            assert "playwright" in cmd

    @_patch_overlays(_INFER_PROJECT_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runner_inferred_from_test_dir(self) -> None:
        mock_result = MagicMock(returncode=0)
        with (
            patch.object(e2e_mod, "resolve_worktree", return_value=None),
            patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
        ):
            call_command("e2e", "run", docker=False)
        assert "pytest" in mock_run.call_args[0][0]

    @_patch_overlays(_INFER_EXTERNAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runner_inferred_from_project_path(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"T3_PRIVATE_TESTS": tmp, "BASE_URL": "https://dev.example"}, clear=False),
        ):
            mock_result = MagicMock(returncode=0)
            with patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run:
                call_command("e2e", "run")
            assert "playwright" in mock_run.call_args[0][0]

    @_patch_overlays(_UNCONFIGURED_OVERLAY)
    @override_settings(**SETTINGS)
    def test_missing_config_exits_with_error(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            call_command("e2e", "run")
        assert exc_info.value.code == 2

    @_patch_overlays(_EXTERNAL_RUNNER_OVERLAY)
    @override_settings(**SETTINGS)
    def test_run_threads_branch_to_external_runner(self) -> None:
        """``e2e run --branch`` forwards the ref through ``_dispatch_runner`` to ``external``."""
        with patch.object(e2e_mod.Command, "external", return_value="E2E passed.") as mock_external:
            call_command("e2e", "run", branch="mr/working-branch")
        assert mock_external.call_args.kwargs["branch"] == "mr/working-branch"


# ── _clone_or_update_e2e_repo ─────────────────────────────────────────


class TestCloneOrUpdateE2eRepo(TestCase):
    def _make_repo(self, *, e2e_dir: str = "e2e") -> "config_mod.E2ERepo":
        return config_mod.E2ERepo(
            name="demo-svc",
            url="git@example.com:org/svc.git",
            branch="feature/e2e",
            e2e_dir=e2e_dir,
        )

    def test_clone_when_not_exists(self) -> None:
        """Calls git clone when cache directory does not exist."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cache_path = tmp_path / "e2e-repos" / "demo-svc"

            with (
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                patch.object(utils_run_mod.subprocess, "run", return_value=MagicMock(returncode=0)) as mock_run,
            ):
                e2e_mod._clone_or_update_e2e_repo(self._make_repo())

            call_args = mock_run.call_args[0][0]
            assert "clone" in call_args
            assert "feature/e2e" in call_args
            assert str(cache_path) in call_args

    def test_fetch_reset_when_exists(self) -> None:
        """Calls git fetch + reset when cache directory already exists."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cache_path = tmp_path / "e2e-repos" / "demo-svc"
            cache_path.mkdir(parents=True)

            calls: list[list[str]] = []

            def capture_run(cmd: list[str], **_kwargs: object) -> MagicMock:
                calls.append(cmd)
                return MagicMock(returncode=0)

            with (
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                patch.object(utils_run_mod.subprocess, "run", side_effect=capture_run),
            ):
                e2e_mod._clone_or_update_e2e_repo(self._make_repo())

            assert any("fetch" in cmd for cmd in calls)
            assert any("reset" in cmd for cmd in calls)

    def test_returns_playwright_root(self) -> None:
        """Returns cache_path / e2e_dir as the playwright working directory."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "e2e-repos" / "demo-svc").mkdir(parents=True)

            with (
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                patch.object(utils_run_mod.subprocess, "run", return_value=MagicMock(returncode=0)),
            ):
                result = e2e_mod._clone_or_update_e2e_repo(self._make_repo(e2e_dir="playwright"))

            assert result == tmp_path / "e2e-repos" / "demo-svc" / "playwright"

    def test_default_ref_is_repo_branch(self) -> None:
        """With no override, the cloned ref is ``repo.branch`` (back-compat)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with (
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                patch.object(utils_run_mod.subprocess, "run", return_value=MagicMock(returncode=0)) as mock_run,
            ):
                e2e_mod._clone_or_update_e2e_repo(self._make_repo())
            call_args = mock_run.call_args[0][0]
            assert "feature/e2e" in call_args

    def test_branch_override_replaces_repo_branch(self) -> None:
        """``branch_override`` checks out a real working branch, not ``repo.branch``."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream = _make_upstream_with_branches(tmp_path, ("feature/e2e", "mr/working-branch"))

            repo = config_mod.E2ERepo(name="demo-svc", url=str(upstream), branch="feature/e2e")
            with patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"):
                root = e2e_mod._clone_or_update_e2e_repo(repo, "mr/working-branch")

            head = subprocess.run(
                [_GIT, "-C", str(root.parent), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert head == "mr/working-branch"

    def test_missing_branch_raises_branch_not_found(self) -> None:
        """A ref absent from the remote raises ``E2eBranchNotFoundError`` (clear message)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream = _make_upstream_with_branches(tmp_path, ("feature/e2e",))

            repo = config_mod.E2ERepo(name="demo-svc", url=str(upstream), branch="feature/e2e")
            with (
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                pytest.raises(e2e_mod.E2eBranchNotFoundError) as exc_info,
            ):
                e2e_mod._clone_or_update_e2e_repo(repo, "no-such-branch")
            assert "no-such-branch" in str(exc_info.value)

    def test_ensure_external_e2e_dependencies_runs_npm_ci_for_locked_project(self) -> None:
        """Managed external clones install their Playwright project deps before running."""
        with tempfile.TemporaryDirectory() as tmp:
            playwright_root = Path(tmp) / "clone" / "e2e"
            playwright_root.mkdir(parents=True)
            (playwright_root / "package.json").write_text('{"scripts":{}}\n')
            (playwright_root / "package-lock.json").write_text("{}\n")

            with patch.object(e2e_runners_mod, "run_checked") as mock_run:
                e2e_runners_mod.ensure_external_e2e_dependencies(playwright_root)

            mock_run.assert_called_once_with(["npm", "ci"], cwd=playwright_root)

    def test_ensure_external_e2e_dependencies_skips_populated_node_modules(self) -> None:
        """A populated dependency directory is reused instead of reinstalling on every run."""
        with tempfile.TemporaryDirectory() as tmp:
            playwright_root = Path(tmp) / "clone" / "e2e"
            package_dir = playwright_root / "node_modules" / "@playwright"
            package_dir.mkdir(parents=True)
            (playwright_root / "package.json").write_text('{"scripts":{}}\n')

            with patch.object(e2e_runners_mod, "run_checked") as mock_run:
                e2e_runners_mod.ensure_external_e2e_dependencies(playwright_root)

            mock_run.assert_not_called()


# ── overlay_e2e_repo (the get_e2e_config -> E2ERepo builder) ───────────


class TestOverlayE2eRepo(TestCase):
    """``overlay_e2e_repo`` lifts an overlay's ``get_e2e_config`` into an ``E2ERepo``."""

    def test_url_and_ref_build_an_e2e_repo(self) -> None:
        """A config with both ``url`` and ``ref`` yields an ``E2ERepo`` carrying them."""
        repo = e2e_runners_mod.overlay_e2e_repo(
            {
                "project_path": "org-eng/client-workspace",
                "url": "git@example.com:org-eng/client-workspace.git",
                "ref": "migration-branch",
                "e2e_dir": "e2e",
            },
        )
        assert repo is not None
        assert repo.name == "client-workspace"
        assert repo.url == "git@example.com:org-eng/client-workspace.git"
        assert repo.branch == "migration-branch"
        assert repo.e2e_dir == "e2e"

    def test_name_falls_back_to_overlay_e2e_without_project_path(self) -> None:
        """No ``project_path`` still builds a repo, named ``overlay-e2e``."""
        repo = e2e_runners_mod.overlay_e2e_repo(
            {"url": "git@example.com:org/svc.git", "ref": "main"},
        )
        assert repo is not None
        assert repo.name == "overlay-e2e"
        assert repo.e2e_dir == "e2e"

    def test_missing_url_returns_none(self) -> None:
        """No ``url`` (e.g. the trigger-ci-only shape) keeps the legacy path: ``None``."""
        assert e2e_runners_mod.overlay_e2e_repo({"project_path": "org/svc", "ref": "main"}) is None

    def test_missing_ref_returns_none(self) -> None:
        """A ``url`` with no ``ref`` cannot pin a checkout — ``None``."""
        assert e2e_runners_mod.overlay_e2e_repo({"url": "git@example.com:org/svc.git"}) is None

    def test_empty_config_returns_none(self) -> None:
        """An overlay with no e2e config (``{}``) yields ``None`` (legacy behaviour)."""
        assert e2e_runners_mod.overlay_e2e_repo({}) is None


# ── resolve_external_specs_path (overlay_repo branch) ─────────────────


class TestResolveExternalSpecsPathOverlayRepo(TestCase):
    """``resolve_external_specs_path`` clones the overlay's own repo when supplied."""

    def test_overlay_repo_clones_at_ref_when_no_named_repo(self) -> None:
        """With ``overlay_repo`` and no ``--repo``, the overlay repo clones at its ``ref``."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream = _make_upstream_with_branches(tmp_path, ("migration-branch",))
            overlay_repo = config_mod.E2ERepo(
                name="client-workspace",
                url=str(upstream),
                branch="migration-branch",
                e2e_dir="e2e",
            )

            with (
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                patch.object(e2e_runners_mod, "ensure_external_e2e_dependencies") as mock_install,
            ):
                root = e2e_runners_mod.resolve_external_specs_path("", "", overlay_repo=overlay_repo)

            assert root == tmp_path / "e2e-repos" / "client-workspace" / "e2e"
            mock_install.assert_called_once_with(root)
            head = subprocess.run(
                [_GIT, "-C", str(root.parent), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert head == "migration-branch"

    def test_branch_override_wins_over_overlay_ref(self) -> None:
        """A ``--branch``/``--ref`` override checks out that branch, not the overlay's ``ref``."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream = _make_upstream_with_branches(tmp_path, ("migration-branch", "open-mr-branch"))
            overlay_repo = config_mod.E2ERepo(
                name="client-workspace",
                url=str(upstream),
                branch="migration-branch",
                e2e_dir="e2e",
            )

            with (
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                patch.object(e2e_runners_mod, "ensure_external_e2e_dependencies"),
            ):
                root = e2e_runners_mod.resolve_external_specs_path("", "open-mr-branch", overlay_repo=overlay_repo)

            head = subprocess.run(
                [_GIT, "-C", str(root.parent), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            assert head == "open-mr-branch"

    def test_named_repo_takes_precedence_over_overlay_repo(self) -> None:
        """An explicit ``--repo`` still wins — the overlay repo is only the default."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            named_base = tmp_path / "named-base"
            named_base.mkdir()
            overlay_base = tmp_path / "overlay-base"
            overlay_base.mkdir()
            named = _make_upstream_with_branches(named_base, ("named-branch",))
            overlay_up = _make_upstream_with_branches(overlay_base, ("migration-branch",))
            named_repo = config_mod.E2ERepo(name="named-svc", url=str(named), branch="named-branch")
            overlay_repo = config_mod.E2ERepo(name="client-workspace", url=str(overlay_up), branch="migration-branch")

            with (
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                patch.object(e2e_runners_mod, "load_e2e_repos", return_value=[named_repo]),
                patch.object(e2e_runners_mod, "ensure_external_e2e_dependencies"),
            ):
                root = e2e_runners_mod.resolve_external_specs_path("named-svc", "", overlay_repo=overlay_repo)

            assert root.parent.name == "named-svc"

    def test_no_overlay_repo_keeps_private_tests_fallback(self) -> None:
        """``overlay_repo=None`` is the legacy path: the ``T3_PRIVATE_TESTS`` fallback is unchanged.

        Regression guard — overlays that supply no ``url`` must behave exactly as before.
        """
        with tempfile.TemporaryDirectory() as tmp:
            private_dir = Path(tmp) / "private"
            private_dir.mkdir()
            with patch.object(e2e_runners_mod, "resolve_private_tests_path", return_value=private_dir):
                root = e2e_runners_mod.resolve_external_specs_path("", "", overlay_repo=None)
            assert root == private_dir

    def test_no_overlay_repo_branch_without_repo_still_rejected(self) -> None:
        """``--branch`` with no ``--repo`` and no overlay_repo is still a misuse (exit 2)."""
        with pytest.raises(e2e_runners_mod.E2eSpecsResolutionError) as exc_info:
            e2e_runners_mod.resolve_external_specs_path("", "some-branch", overlay_repo=None)
        assert exc_info.value.exit_code == 2


# ── e2e external --repo ───────────────────────────────────────────────


class TestE2eExternalRepo(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_external_repo_not_found_in_config_raises_system_exit_1(self) -> None:
        """A named E2E repo not in config is a misconfig — exit 1.

        Regression for #932.
        """
        with (
            patch.object(e2e_runners_mod, "load_e2e_repos", return_value=[]),
            pytest.raises(SystemExit) as exc_info,
        ):
            call_command("e2e", "external", repo="nonexistent")
        assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_external_repo_uses_cloned_path(self) -> None:
        """Playwright runs from the cloned repo's e2e_dir when --repo is given."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            playwright_root = tmp_path / "clone" / "e2e"
            playwright_root.mkdir(parents=True)

            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/repo-e2e")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )

            repo = config_mod.E2ERepo(name="demo-svc", url="git@example.com:org/svc.git", branch="feature/e2e")
            mock_result = MagicMock(returncode=0)
            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": str(wt_dir)}),
                patch.object(e2e_runners_mod, "load_e2e_repos", return_value=[repo]),
                patch.object(e2e_runners_mod, "clone_or_update_e2e_repo", return_value=playwright_root),
                patch.object(e2e_runners_mod, "ensure_external_e2e_dependencies") as mock_install,
                patch.object(e2e_disc_mod, "get_service_port", return_value=4200),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                result = cast("str", call_command("e2e", "external", repo="demo-svc"))

        assert "passed" in result
        mock_install.assert_called_once_with(playwright_root)
        run_cwd = mock_run.call_args[1]["cwd"]
        assert str(run_cwd) == str(playwright_root)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_private_tests_path_does_not_auto_install_dependencies(self) -> None:
        """User-managed ``T3_PRIVATE_TESTS`` checkouts remain outside TeaTree's install policy."""
        with tempfile.TemporaryDirectory() as tmp:
            private_dir = Path(tmp) / "private"
            private_dir.mkdir()
            wt_dir = Path(tmp) / "worktree"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/private-e2e")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )

            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": str(wt_dir), "T3_PRIVATE_TESTS": str(private_dir)}),
                patch.object(e2e_runners_mod, "ensure_external_e2e_dependencies") as mock_install,
                patch.object(e2e_disc_mod, "get_service_port", return_value=4200),
                patch.object(utils_run_mod, "Popen", _popen_for(MagicMock(returncode=0))),
            ):
                call_command("e2e", "external")

        mock_install.assert_not_called()

    def _run_external_capturing_ref(self, captured: dict[str, str], **call_kwargs: object) -> None:
        """Drive ``e2e external --repo`` with a stubbed clone that records the ref override."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            playwright_root = tmp_path / "clone" / "e2e"
            playwright_root.mkdir(parents=True)
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/branch-ref")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )

            def fake_clone(_repo: object, branch_override: str = "") -> Path:
                captured["ref"] = branch_override
                return playwright_root

            repo = config_mod.E2ERepo(name="demo-svc", url="git@example.com:org/svc.git", branch="feature/e2e")
            with (
                patch.dict("os.environ", {"T3_ORIG_CWD": str(wt_dir)}),
                patch.object(e2e_runners_mod, "load_e2e_repos", return_value=[repo]),
                patch.object(e2e_runners_mod, "clone_or_update_e2e_repo", side_effect=fake_clone),
                patch.object(e2e_disc_mod, "get_service_port", return_value=4200),
                patch.object(utils_run_mod, "Popen", _popen_for(MagicMock(returncode=0))),
            ):
                call_command("e2e", "external", repo="demo-svc", **call_kwargs)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_branch_option_threads_to_clone(self) -> None:
        """``--branch`` is forwarded to the clone as the specs ref override."""
        captured: dict[str, str] = {}
        self._run_external_capturing_ref(captured, branch="mr/working-branch")
        assert captured["ref"] == "mr/working-branch"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_branch_preserves_default_ref(self) -> None:
        """Omitting ``--branch`` forwards an empty override — clone keeps ``repo.branch``."""
        captured: dict[str, str] = {}
        self._run_external_capturing_ref(captured)
        assert captured["ref"] == ""

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_branch_not_found_exits_1_with_message(self) -> None:
        """A missing specs ref surfaces ``E2eBranchNotFoundError`` as a clean exit 1."""
        repo = config_mod.E2ERepo(name="demo-svc", url="git@example.com:org/svc.git", branch="feature/e2e")
        with (
            patch.object(e2e_runners_mod, "load_e2e_repos", return_value=[repo]),
            patch.object(
                e2e_runners_mod,
                "clone_or_update_e2e_repo",
                side_effect=e2e_mod.E2eBranchNotFoundError(name="demo-svc", ref="gone", url="git@x:o/s.git"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            call_command("e2e", "external", repo="demo-svc", branch="gone")
        assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_branch_without_repo_is_rejected(self) -> None:
        """``--branch`` against the T3_PRIVATE_TESTS path is a misuse — exit 2."""
        with (
            patch.object(e2e_runners_mod, "resolve_private_tests_path", return_value=Path("/tmp/specs")),
            pytest.raises(SystemExit) as exc_info,
        ):
            call_command("e2e", "external", branch="mr/working-branch")
        assert exc_info.value.code == 2

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_external_repo_git_failure_surfaces_error(self) -> None:
        """subprocess.CalledProcessError from git is raised to the caller."""
        repo = config_mod.E2ERepo(name="demo-svc", url="git@example.com:org/svc.git", branch="feature/e2e")
        git_failure = subprocess.CalledProcessError(1, "git")
        with (
            patch.object(e2e_runners_mod, "load_e2e_repos", return_value=[repo]),
            patch.object(e2e_runners_mod, "clone_or_update_e2e_repo", side_effect=git_failure),
            pytest.raises(subprocess.CalledProcessError),
        ):
            call_command("e2e", "external", repo="demo-svc")

    @_patch_overlays(_OVERLAY_REPO_OVERLAY)
    @override_settings(**SETTINGS)
    def test_external_uses_overlay_repo_at_ref_without_named_repo(self) -> None:
        """The overlay's own ``get_e2e_config`` repo clones at its ``ref`` with no ``--repo``.

        End-to-end through ``e2e external`` → ``get_e2e_config`` → ``overlay_e2e_repo``
        → ``resolve_external_specs_path``: the runner sources the suite from the
        overlay-declared repo+ref, never from ``T3_PRIVATE_TESTS``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream = _make_upstream_with_branches(tmp_path, ("migration-branch",))
            wt_dir = tmp_path / "worktree"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/overlay-repo-e2e")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
                state=Worktree.State.SERVICES_UP,
            )

            mock_result = MagicMock(returncode=0)
            with (
                patch.dict(
                    "os.environ",
                    {"T3_ORIG_CWD": str(wt_dir), "T3_TEST_OVERLAY_E2E_URL": str(upstream)},
                ),
                patch.object(e2e_runners_mod, "get_data_dir", return_value=tmp_path / "e2e-repos"),
                patch.object(e2e_runners_mod, "ensure_external_e2e_dependencies"),
                patch.object(e2e_disc_mod, "get_service_port", return_value=4200),
                patch.object(utils_run_mod, "Popen", _popen_for(mock_result)) as mock_run,
            ):
                result = cast("str", call_command("e2e", "external"))

                assert "passed" in result
                run_cwd = Path(mock_run.call_args[1]["cwd"])
                assert run_cwd == tmp_path / "e2e-repos" / "client-workspace" / "e2e"
                head = subprocess.run(
                    [_GIT, "-C", str(run_cwd.parent), "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                assert head == "migration-branch"


class TestE2EResolveTarget(TestCase):
    """`--target` resolution is deterministic and drives `T3_E2E_TARGET`."""

    def test_explicit_values_are_normalized(self) -> None:
        cmd = e2e_mod.Command()
        for raw, expected in [
            ("dev", "dev"),
            ("qa", "qa"),
            ("local", "local"),
            ("DEV", "dev"),
            (" QA ", "qa"),
            (" Local ", "local"),
        ]:
            with self.subTest(raw=raw):
                assert cmd._resolve_target(raw) == expected

    def test_invalid_value_exits(self) -> None:
        with pytest.raises(SystemExit):
            e2e_mod.Command()._resolve_target("staging")

    def test_empty_infers_from_base_url(self) -> None:
        cmd = e2e_mod.Command()
        with patch.dict(os.environ, {"BASE_URL": "https://app-development.example.com"}, clear=False):
            assert cmd._resolve_target("") == "dev"
        env_no_base = {k: v for k, v in os.environ.items() if k != "BASE_URL"}
        with patch.dict(os.environ, env_no_base, clear=True):
            assert cmd._resolve_target("") == "local"

    def test_build_env_exports_t3_e2e_target(self) -> None:
        with (
            patch.object(e2e_runners_mod, "get_overlay") as get_overlay,
            patch.object(e2e_runners_mod, "_find_env_cache", return_value=None),
        ):
            get_overlay.return_value.e2e.env_extras.return_value = {}
            env = e2e_mod._build_e2e_env("https://tenant-qa.example.com", headed=False, target="qa")
        assert env["T3_E2E_TARGET"] == "qa"
        assert env["BASE_URL"] == "https://tenant-qa.example.com"
        assert env["CI"] == "1"
