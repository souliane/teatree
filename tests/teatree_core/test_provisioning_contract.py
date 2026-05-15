"""End-to-end provisioning-workflow contract.

The recurring provisioning-bug class (a new one found manually almost daily)
all share one shape: the orchestration *logic* has branch coverage, but the
*provisioned, serving reality* is never asserted, so seam defects surface
only when a human runs a real stack.

This pins, generically and CI-safe (no docker / npm / nx — step callables
just touch real files under tmp_path), the invariants every one of those
bugs violated. Any overlay that reintroduces the class fails here before
merge instead of in a production-like run.
"""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import ProvisionStep, RunCommands
from teatree.core.runners.service_launch import ServiceLauncher
from teatree.types import RunCommand
from tests.teatree_core.conftest import CommandOverlay


class FullStackOverlay(CommandOverlay):
    """Generic 3-runtime stack: a backend, a microservice, a frontend.

    Step callables append to one file so the integrated provision/start/run
    chain's real ordering can be asserted. No real docker/nx/npm.
    """

    SERVICES: ClassVar[tuple[str, ...]] = ("backend", "microservice", "frontend")

    def __init__(self, order_file: Path) -> None:
        self.order_file = order_file

    def get_repos(self) -> list[str]:
        return ["backend", "microservice", "frontend"]

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        # argv[0] is always a real executable, never a "KEY=value" env
        # assignment (the class of bug where a shell-style env prefix is
        # passed to a no-shell exec and becomes argv[0]).
        return {svc: RunCommand(args=["sh", "-lc", f"echo run-{svc} >> {self.order_file}"]) for svc in self.SERVICES}

    def get_pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        # Prereqs are a property of the *service*, never of which worktree
        # happens to be provisioned/tracked (the gating anti-pattern that
        # left a backend-tracked workspace's frontend unprovisioned).
        def record() -> None:
            with self.order_file.open("a") as fh:
                fh.write(f"prereq-{service}\n")

        return [ProvisionStep(name=f"prereq-{service}", callable=record)]


class ProvisioningContractTests(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")
        # The tracked worktree is the BACKEND — the common shape that exposed
        # the "frontend setup gated on tracked worktree" bugs.
        self.worktree = Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b")
        self._tmp = tempfile.TemporaryDirectory()
        self.order_file = Path(self._tmp.name) / "order.txt"
        self.overlay = FullStackOverlay(self.order_file)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # INV1 — every service runs its prereqs before its command, via the one
    # launcher, for every service shape (the run.py-skipped-pre-run class).
    def test_every_service_runs_prereqs_before_command(self) -> None:
        for svc in FullStackOverlay.SERVICES:
            self.order_file.write_text("")
            result = ServiceLauncher(self.worktree, svc, overlay=self.overlay).run()
            assert result.ok, svc
            assert self.order_file.read_text().splitlines() == [f"prereq-{svc}", f"run-{svc}"], svc

    # INV1 (worktree-start path) — prepare_all wires every service's prereqs.
    def test_worktree_start_path_prepares_all_services(self) -> None:
        ServiceLauncher.prepare_all(self.worktree, list(FullStackOverlay.SERVICES), overlay=self.overlay)
        ran = sorted(self.order_file.read_text().splitlines())
        assert ran == ["prereq-backend", "prereq-frontend", "prereq-microservice"]
        assert not any(line.startswith("run-") for line in self.order_file.read_text().splitlines())

    # INV2 — no run-command argv[0] is a shell-style env assignment.
    def test_run_command_argv0_is_executable_not_env_assignment(self) -> None:
        commands = self.overlay.get_run_commands(self.worktree)
        for svc, cmd in commands.items():
            argv0 = cmd.args[0]
            assert "=" not in argv0, f"{svc}: argv[0] {argv0!r} looks like an env assignment"

    # INV3 — a backend-tracked workspace still produces the frontend's
    # prereqs (setup is per-service, not gated on the tracked worktree).
    def test_frontend_prereqs_exist_when_backend_is_tracked(self) -> None:
        assert self.worktree.repo_path == "backend"
        steps = self.overlay.get_pre_run_steps(self.worktree, "frontend")
        assert [s.name for s in steps] == ["prereq-frontend"]

    # INV4 — get_e2e_env_extras is honored; CUSTOMER bridges WT_VARIANT.
    def test_e2e_env_extras_bridge_variant(self) -> None:
        assert self.overlay.get_e2e_env_extras({"WT_VARIANT": "acme"}) == {"CUSTOMER": "acme"}

    def test_missing_service_is_not_ok_but_prereqs_still_ran(self) -> None:
        wt = SimpleNamespace(repo_path="backend", branch="b", db_name="", extra={}, ticket=None)
        result = ServiceLauncher(wt, "nope", overlay=self.overlay).run()
        assert not result.ok
        assert self.order_file.read_text().splitlines() == ["prereq-nope"]
