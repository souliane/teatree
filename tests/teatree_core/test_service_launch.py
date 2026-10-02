import tempfile
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from django.test import TestCase

import teatree.utils.singleton as singleton_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayRuntime, ProvisionStep, RunCommands
from teatree.core.runners.service_launch import ServiceLauncher
from teatree.types import RunCommand
from teatree.utils.singleton import singleton
from tests.teatree_core.conftest import CommandOverlay


class _OrderRecordingOverlayRuntime(OverlayRuntime):
    def __init__(self, overlay: "OrderRecordingOverlay") -> None:
        self._overlay = overlay

    def run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "frontend": RunCommand(args=["sh", "-lc", f"echo cmd >> {self._overlay.order_file}"]),
            "backend": ["true"],
        }

    def pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def record() -> None:
            with self._overlay.order_file.open("a") as fh:
                fh.write(f"pre-{service}\n")

        return [ProvisionStep(name=f"pre-run-{service}", callable=record)]


class OrderRecordingOverlay(CommandOverlay):
    """Pre-run step and command both append to one file to prove ordering."""

    def __init__(self, order_file: Path) -> None:
        self.order_file = order_file
        self.runtime = _OrderRecordingOverlayRuntime(self)


class ServiceLauncherTests(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")
        self.worktree = Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b")
        self._tmp = tempfile.TemporaryDirectory()
        self.order_file = Path(self._tmp.name) / "order.txt"
        self.overlay = OrderRecordingOverlay(self.order_file)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_executes_pre_run_steps_before_command(self) -> None:
        result = ServiceLauncher(self.worktree, "frontend", overlay=self.overlay).run()
        assert result.ok
        assert self.order_file.read_text().splitlines() == ["pre-frontend", "cmd"]

    def test_run_returns_not_ok_when_service_has_no_command(self) -> None:
        result = ServiceLauncher(self.worktree, "missing", overlay=self.overlay).run()
        assert not result.ok
        assert "missing" in result.detail
        # Pre-run still ran — prerequisites are never conditional on the command.
        assert self.order_file.read_text().splitlines() == ["pre-missing"]

    def test_prepare_all_runs_pre_run_for_every_service(self) -> None:
        ServiceLauncher.prepare_all(self.worktree, ["frontend", "backend"], overlay=self.overlay)
        assert self.order_file.read_text().splitlines() == ["pre-frontend", "pre-backend"]

    def test_single_flight_refuses_concurrent_same_service_build(self) -> None:
        """A second launch of the same (worktree, service) refuses while one holds the lock (#1038).

        Anti-vacuity: drop the ``singleton(...)`` wrapper from ``run()`` and this
        goes RED — the second launch executes the command instead of refusing.
        """
        launcher = ServiceLauncher(self.worktree, "frontend", overlay=self.overlay)
        # Isolate the lock dir from other xdist workers running the same test, then
        # hold the exact per-(worktree, service) lock to simulate a build in flight.
        with patch.object(singleton_mod, "DATA_DIR", Path(self._tmp.name)), singleton(launcher._lock_name()):
            result = launcher.run()
        assert not result.ok
        assert "already in flight" in result.detail
        # The competing build never ran: neither pre-run nor command executed, so
        # the order file was never even created.
        assert not self.order_file.exists()

    def test_single_flight_allows_different_service_concurrently(self) -> None:
        """Holding the frontend lock must NOT block a different service on the same worktree."""
        held = ServiceLauncher(self.worktree, "frontend", overlay=self.overlay)
        with patch.object(singleton_mod, "DATA_DIR", Path(self._tmp.name)), singleton(held._lock_name()):
            result = ServiceLauncher(self.worktree, "backend", overlay=self.overlay).run()
        assert result.ok


class _RealisticStackOverlayRuntime(OverlayRuntime):
    def __init__(self, overlay: "RealisticStackOverlay") -> None:
        self._overlay = overlay

    def run_commands(self, worktree: Worktree) -> RunCommands:
        overlay = self._overlay
        return {
            svc: RunCommand(args=["sh", "-lc", f"echo run-{svc} >> {overlay.order_file}"]) for svc in overlay.PREREQS
        }

    def pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def make(step_name: str) -> ProvisionStep:
            def record() -> None:
                with self._overlay.order_file.open("a") as fh:
                    fh.write(f"{step_name}\n")

            return ProvisionStep(name=f"{service}-{step_name}", callable=record)

        return [make(name) for name in self._overlay.PREREQS.get(service, [])]


class RealisticStackOverlay(CommandOverlay):
    """Generic realistic stack: a django backend, a fastapi microservice, a frontend."""

    PREREQS: ClassVar[dict[str, list[str]]] = {
        "backend": ["migrate"],
        "microservice": ["uvicorn-deps"],
        "frontend": ["npm-install", "link-node-modules"],
    }

    def __init__(self, order_file: Path) -> None:
        self.order_file = order_file
        self.runtime = _RealisticStackOverlayRuntime(self)


class RealisticStackWorkflowTests(TestCase):
    """Drift-net: prereqs-then-command holds for every service shape, both entry points."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2")
        self.worktree = Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b")
        self._tmp = tempfile.TemporaryDirectory()
        self.order_file = Path(self._tmp.name) / "order.txt"
        self.overlay = RealisticStackOverlay(self.order_file)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_each_service_runs_its_prereqs_before_its_command(self) -> None:
        for svc, prereqs in RealisticStackOverlay.PREREQS.items():
            self.order_file.write_text("")
            result = ServiceLauncher(self.worktree, svc, overlay=self.overlay).run()
            assert result.ok, svc
            assert self.order_file.read_text().splitlines() == [*prereqs, f"run-{svc}"], svc

    def test_worktree_start_path_prepares_every_service(self) -> None:
        ServiceLauncher.prepare_all(self.worktree, list(RealisticStackOverlay.PREREQS), overlay=self.overlay)
        ran = set(self.order_file.read_text().splitlines())
        assert ran == {"migrate", "uvicorn-deps", "npm-install", "link-node-modules"}
        # No run-* markers — prepare_all wires prerequisites, never commands.
        assert not any(line.startswith("run-") for line in self.order_file.read_text().splitlines())


class _EnvOverlayRuntime(OverlayRuntime):
    def __init__(self, overlay: "EnvOverlay") -> None:
        self._overlay = overlay

    def run_commands(self, worktree: Worktree) -> RunCommands:
        out = self._overlay.out_file
        # The command echoes an env var the overlay supplied via RunCommand.env
        # — proving the runner merged cmd.env into the subprocess environment.
        script = f'printf "%s" "$OVERLAY_VAR" > {out}'
        return {"backend": RunCommand(args=["sh", "-c", script], env={"OVERLAY_VAR": "merged"})}

    def pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        return []


class EnvOverlay(CommandOverlay):
    """An overlay whose run command declares env directly on the RunCommand."""

    def __init__(self, out_file: Path) -> None:
        self.out_file = out_file
        self.runtime = _EnvOverlayRuntime(self)


class ServiceLauncherEnvTests(TestCase):
    """RunCommand.env is merged into the subprocess environment when exec'ing argv (#3330)."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3")
        self.worktree = Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b")
        self._tmp = tempfile.TemporaryDirectory()
        self.out_file = Path(self._tmp.name) / "env.txt"
        self.overlay = EnvOverlay(self.out_file)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_command_env_reaches_the_subprocess(self) -> None:
        result = ServiceLauncher(self.worktree, "backend", overlay=self.overlay).run()
        assert result.ok
        assert self.out_file.read_text() == "merged"
