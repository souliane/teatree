"""Time-box + loud-alert guards for long-blocking provisioning steps (#2220).

A `worktree provision` / `start` step (DSLR restore, `migrate`, `--create-db`
test-DB rebuild) must never hang silently: it is bounded by a configurable
ceiling, and on a timeout — or on a forked migration graph detected in the
step's output — it fails loud AND fires an out-of-band user alert naming the
slow/diagnosed step, with a progress heartbeat distinguishing slow-but-moving
from a true hang.
"""

import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase, override_settings

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.provision.provision_timebox import (
    DEFAULT_FAST_STEP_TIMEOUT_SECONDS,
    DEFAULT_STEP_TIMEOUT_SECONDS,
    ProgressAlert,
    alert_provision_user,
    detect_migration_conflict,
    resolve_step_timeout_seconds,
    run_timeboxed_callable,
    run_timeboxed_db_import,
    run_timeboxed_step,
)
from teatree.core.provision.step_runner import run_step


class TestDetectMigrationConflict(TestCase):
    """The forked-graph detector: True on a conflict, False on a linear graph."""

    def test_conflicting_migrations_phrase(self) -> None:
        out = "CommandError: Conflicting migrations detected; multiple leaf nodes in the migration graph"
        conflict = detect_migration_conflict(out)
        assert conflict is not None
        assert "core" in conflict or conflict  # truthy diagnosis

    def test_multiple_leaf_nodes_phrase(self) -> None:
        out = "multiple leaf nodes in the migration graph (0045_a, 0045_b in core)"
        assert detect_migration_conflict(out) is not None

    def test_linear_output_is_not_a_conflict(self) -> None:
        out = "Operations to perform:\n  Apply all migrations: core\nRunning migrations:\n  No migrations to apply."
        assert detect_migration_conflict(out) is None

    def test_empty_output_is_not_a_conflict(self) -> None:
        assert detect_migration_conflict("") is None


class TestAlertProvisionUser(TestCase):
    """The #2220 loud alert is OWNER_ESCALATION so it actually leaves the machine (F4.2)."""

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_alert_uses_owner_escalation_audience(self, mock_notify: MagicMock) -> None:
        alert_provision_user(step="migrate", repo="acme/app", detail="exceeded 1800s and was aborted")
        assert mock_notify.called
        # An INTERNAL audience short-circuits notify_user BEFORE any backend
        # resolution — the loud alert would degrade to a log line and never
        # reach the away user. It must be OWNER_ESCALATION.
        assert mock_notify.call_args.kwargs["audience"] is NotifyAudience.OWNER_ESCALATION

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_timeout_alert_is_owner_escalation(self, mock_notify: MagicMock) -> None:
        with patch("teatree.core.provision.provision_timebox.run_allowed_to_fail") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["migrate"], timeout=1)
            run_timeboxed_step("migrate", ["manage.py", "migrate"], timeout=1)
        assert mock_notify.called
        assert mock_notify.call_args.kwargs["audience"] is NotifyAudience.OWNER_ESCALATION


class TestResolveStepTimeout(TestCase):
    """The ceiling is configurable, fast-by-default; ``heavy=True`` opts into the long one (souliane/teatree#2949)."""

    def test_default_is_a_positive_ceiling(self) -> None:
        assert resolve_step_timeout_seconds() > 0

    def test_default_is_the_fast_ceiling(self) -> None:
        assert resolve_step_timeout_seconds() == DEFAULT_FAST_STEP_TIMEOUT_SECONDS

    def test_heavy_is_the_long_ceiling(self) -> None:
        assert resolve_step_timeout_seconds(heavy=True) == DEFAULT_STEP_TIMEOUT_SECONDS

    def test_fast_ceiling_is_shorter_than_heavy(self) -> None:
        assert resolve_step_timeout_seconds() < resolve_step_timeout_seconds(heavy=True)

    @override_settings()
    def test_heavy_override_wins(self) -> None:
        with patch("teatree.core.provision.provision_timebox.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(provision_step_timeout_seconds=42)
            assert resolve_step_timeout_seconds(heavy=True) == 42

    @override_settings()
    def test_fast_override_wins(self) -> None:
        with patch("teatree.core.provision.provision_timebox.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(provision_fast_step_timeout_seconds=7)
            assert resolve_step_timeout_seconds() == 7

    @override_settings()
    def test_non_positive_heavy_value_falls_back_to_default(self) -> None:
        with patch("teatree.core.provision.provision_timebox.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(provision_step_timeout_seconds=0)
            assert resolve_step_timeout_seconds(heavy=True) == DEFAULT_STEP_TIMEOUT_SECONDS


class TestRunTimeboxedStep(TestCase):
    """A timeout fails loud + alerts; a conflict is diagnosed; progress heartbeats."""

    @patch("teatree.core.provision.provision_timebox.notify_user")
    @patch("teatree.core.provision.provision_timebox.run_allowed_to_fail")
    def test_timeout_aborts_and_alerts(self, mock_run: MagicMock, mock_notify: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["migrate"], timeout=1)
        result = run_timeboxed_step("migrate", ["manage.py", "migrate"], timeout=1)
        assert result.success is False
        assert "timed out" in result.error
        assert mock_notify.called
        alert_text = mock_notify.call_args.args[0]
        assert "migrate" in alert_text

    @patch("teatree.core.provision.provision_timebox.notify_user")
    @patch("teatree.core.provision.provision_timebox.run_allowed_to_fail")
    def test_migration_conflict_diagnosed_in_alert(self, mock_run: MagicMock, mock_notify: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="CommandError: Conflicting migrations detected; multiple leaf nodes",
        )
        result = run_timeboxed_step("migrate", ["manage.py", "migrate"], timeout=300)
        assert result.success is False
        assert mock_notify.called
        alert_text = mock_notify.call_args.args[0]
        assert "migration" in alert_text.lower()
        assert "makemigrations --merge" in alert_text

    @patch("teatree.core.provision.provision_timebox.notify_user")
    @patch("teatree.core.provision.provision_timebox.run_allowed_to_fail")
    def test_success_does_not_alert(self, mock_run: MagicMock, mock_notify: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        result = run_timeboxed_step("migrate", ["manage.py", "migrate"], timeout=300)
        assert result.success is True
        assert not mock_notify.called

    @patch("teatree.core.provision.provision_timebox.notify_user")
    @patch("teatree.core.provision.provision_timebox.run_allowed_to_fail")
    def test_heartbeat_fires_while_running(self, mock_run: MagicMock, mock_notify: MagicMock) -> None:
        beats: list[str] = []

        def slow_then_finish(*_args: object, **_kwargs: object) -> MagicMock:
            time.sleep(0.25)
            return MagicMock(returncode=0, stdout="ok", stderr="")

        mock_run.side_effect = slow_then_finish
        run_timeboxed_step(
            "restore",
            ["pg_restore"],
            timeout=300,
            progress=ProgressAlert(interval=0.05, heartbeat=beats.append),
        )
        assert beats, "expected at least one heartbeat while the op ran"
        assert any("restore" in b for b in beats)


class TestRunTimeboxedStepEnvAndStdin(TestCase):
    """`env` and `stdin_text` thread straight through to the subprocess."""

    def test_env_reaches_the_child(self) -> None:
        result = run_timeboxed_step(
            "echo-env", ["sh", "-c", "echo $PROVISION_VAR"], env={"PROVISION_VAR": "on"}, timeout=30
        )
        assert result.success is True
        assert result.stdout.strip() == "on"

    def test_stdin_text_is_piped_in(self) -> None:
        result = run_timeboxed_step("restore", ["cat"], stdin_text="dump-bytes", timeout=30)
        assert result.success is True
        assert result.stdout == "dump-bytes"

    @patch("teatree.core.provision.provision_timebox.run_allowed_to_fail")
    def test_env_and_stdin_forwarded_to_runner(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        run_timeboxed_step("step", ["cmd"], env={"K": "V"}, stdin_text="in", timeout=30)
        assert mock_run.call_args.kwargs["env"] == {"K": "V"}
        assert mock_run.call_args.kwargs["stdin_text"] == "in"


class TestRunStepUsesTimebox(TestCase):
    """`run_step` routes long-blocking steps through the time-box on timeout."""

    @patch("teatree.core.provision.provision_timebox.notify_user")
    @patch("teatree.utils.run.subprocess")
    def test_run_step_timeout_emits_alert(self, mock_sp: MagicMock, mock_notify: MagicMock) -> None:
        mock_sp.run.side_effect = subprocess.TimeoutExpired(cmd=["slow"], timeout=1)
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        result = run_step("migrate", ["manage.py", "migrate"], timeout=1)
        assert result.success is False
        assert "timed out" in result.error
        assert mock_notify.called


class TestRunTimeboxedCallable(TestCase):
    """An ORM-free subprocess callable is wall-clock bounded (#2244).

    The ``subprocess_only`` provision steps (``uv sync`` / ``uv pip install -e``,
    each shelling out) abort loud with a named step when a child blocks on its
    PIPE, never hanging. A clean return is interpreted exactly as
    ``run_callable_step`` does.
    """

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_overrun_aborts_and_names_step(self, mock_notify: MagicMock) -> None:
        release = threading.Event()
        result = run_timeboxed_callable(
            "sync-dependencies", lambda: release.wait(timeout=3), timeout=0.1, progress=ProgressAlert(interval=0.05)
        )
        release.set()
        assert result.success is False
        assert result.name == "sync-dependencies"
        assert "timed out" in result.error
        assert mock_notify.called
        assert "sync-dependencies" in mock_notify.call_args.args[0]

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_clean_completed_process_is_interpreted(self, mock_notify: MagicMock) -> None:
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")
        result = run_timeboxed_callable("sync-dependencies", lambda: ok, timeout=5)
        assert result.success is True
        assert result.stdout == "done"
        assert not mock_notify.called

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_failed_completed_process_is_a_failure(self, mock_notify: MagicMock) -> None:
        bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        result = run_timeboxed_callable("sync-dependencies", lambda: bad, timeout=5)
        assert result.success is False
        assert "boom" in result.error

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_heartbeat_fires_while_running(self, mock_notify: MagicMock) -> None:
        _ = mock_notify
        beats: list[str] = []

        def slow_then_finish() -> None:
            time.sleep(0.2)

        run_timeboxed_callable(
            "sync-dependencies",
            slow_then_finish,
            timeout=5,
            progress=ProgressAlert(interval=0.05, heartbeat=beats.append),
        )
        assert beats, "expected at least one heartbeat while the callable ran"
        assert any("sync-dependencies" in b for b in beats)


class TestRunTimeboxedDbImport(TestCase):
    """The DB-import call is wall-clock bounded (#2244).

    The no-DSLR-snapshot block aborts loud-and-fast with an actionable message
    instead of hanging on a child stuck on its PIPE.
    """

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_passes_through_success(self, mock_notify: MagicMock) -> None:
        assert run_timeboxed_db_import(lambda: True, timeout=5) is True
        assert not mock_notify.called

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_passes_through_failure(self, mock_notify: MagicMock) -> None:
        assert run_timeboxed_db_import(lambda: False, timeout=5) is False
        assert not mock_notify.called

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_overrun_returns_false_with_actionable_alert(self, mock_notify: MagicMock) -> None:
        release = threading.Event()
        result = run_timeboxed_db_import(
            lambda: release.wait(timeout=3) or True, timeout=0.1, progress=ProgressAlert(interval=0.05)
        )
        release.set()
        assert result is False
        assert mock_notify.called
        alert_text = mock_notify.call_args.args[0].lower()
        assert "dslr snapshot" in alert_text
        assert "db refresh" in alert_text

    @patch("teatree.core.provision.provision_timebox.notify_user")
    def test_reraises_a_callable_exception(self, mock_notify: MagicMock) -> None:
        def boom() -> bool:
            msg = "kaboom"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError):
            run_timeboxed_db_import(boom, timeout=5)
        assert not mock_notify.called
