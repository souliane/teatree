"""End-to-end provisioning-workflow contract.

The recurring provisioning-bug class (a new one found manually almost daily)
all share one shape: the orchestration *logic* has branch coverage, but the
*provisioned, serving reality* is never asserted, so seam defects surface
only when a human runs a real stack.

This pins, generically and CI-safe (no docker / npm / nx — step callables
just touch real files under tmp_path, the only mocked seams are the
subprocess/docker boundary and which overlay the loader returns), the
invariants every one of those bugs violated. Any overlay or runner refactor
that reintroduces the class fails here before merge instead of in a
production-like run. Behavior is asserted through public runner/model
entrypoints — a refactor that preserves behavior keeps these green; only a
real regression turns them red.
"""

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.management.commands.e2e import _build_e2e_env
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import ProvisionStep, RunCommands
from teatree.core.runners.service_launch import ServiceLauncher
from teatree.core.runners.worktree_provision import WorktreeProvisionRunner
from teatree.core.runners.worktree_start import WorktreeStartRunner
from teatree.core.step_runner import StepResult
from teatree.types import RunCommand
from tests.teatree_core.conftest import CommandOverlay


class FullStackOverlay(CommandOverlay):
    """Generic 3-runtime stack: a backend, a microservice, a frontend.

    Step callables append to one file so the integrated provision/start/run
    chain's real ordering can be asserted. No real docker/nx/npm.

    ``fail_provision_step`` makes one required provision step raise so the
    abort-on-required-failure contract can be pinned.
    """

    SERVICES: ClassVar[tuple[str, ...]] = ("backend", "microservice", "frontend")
    PROVISION_STEPS: ClassVar[tuple[str, ...]] = ("schema", "seed")

    def __init__(self, order_file: Path, *, fail_provision_step: str | None = None) -> None:
        self.order_file = order_file
        self.fail_provision_step = fail_provision_step

    def _record(self, token: str) -> None:
        with self.order_file.open("a") as fh:
            fh.write(f"{token}\n")

    def get_repos(self) -> list[str]:
        return ["backend", "microservice", "frontend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        steps: list[ProvisionStep] = []
        for name in self.PROVISION_STEPS:

            def make(step_name: str) -> ProvisionStep:
                def run() -> None:
                    self._record(f"prov-{step_name}")
                    if step_name == self.fail_provision_step:
                        msg = f"{step_name} blew up"
                        raise RuntimeError(msg)

                return ProvisionStep(name=f"prov-{step_name}", callable=run)

            steps.append(make(name))
        return steps

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
            self._record(f"prereq-{service}")

        return [ProvisionStep(name=f"prereq-{service}", callable=record)]


def _provisioned_worktree(tmp: str, ticket: Ticket) -> Worktree:
    """A worktree whose on-disk path exists so write_env_cache works for real."""
    wt_path = Path(tmp) / "ticket" / "backend"
    wt_path.mkdir(parents=True, exist_ok=True)
    return Worktree.objects.create(
        ticket=ticket,
        repo_path="backend",
        branch="b",
        db_name="",
        extra={"worktree_path": str(wt_path)},
    )


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


class _SharedPrereqOverlay(FullStackOverlay):
    """Two services share a prereq step name — dedup must collapse it."""

    def get_pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def shared() -> None:
            self._record("prereq-shared")

        def per_service() -> None:
            self._record(f"prereq-{service}")

        return [
            ProvisionStep(name="prereq-shared", callable=shared),
            ProvisionStep(name=f"prereq-{service}", callable=per_service),
        ]


class _FailingPrereqOverlay(FullStackOverlay):
    """The backend prereq raises — later services must still be prepared."""

    def get_pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def record() -> None:
            self._record(f"prereq-{service}")
            if service == "backend":
                msg = "backend prereq blew up"
                raise RuntimeError(msg)

        return [ProvisionStep(name=f"prereq-{service}", callable=record)]


class ServiceLauncherContractTests(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2")
        self.worktree = Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b")
        self._tmp = tempfile.TemporaryDirectory()
        self.order_file = Path(self._tmp.name) / "order.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # prepare_all dedups a prereq shared across services to exactly one run
    # (the _collect_steps `seen` set — re-running a shared setup N times was
    # both wasteful and a source of order-dependent corruption).
    def test_prepare_all_dedups_shared_prereq(self) -> None:
        overlay = _SharedPrereqOverlay(self.order_file)
        ServiceLauncher.prepare_all(self.worktree, list(FullStackOverlay.SERVICES), overlay=overlay)
        lines = self.order_file.read_text().splitlines()
        assert lines.count("prereq-shared") == 1
        assert sorted(line for line in lines if line != "prereq-shared") == [
            "prereq-backend",
            "prereq-frontend",
            "prereq-microservice",
        ]

    # prepare_all is best-effort: a failing prereq for one service must NOT
    # halt the others (stop_on_required_failure=False at this call site).
    # Flipping that to fail-fast would silently leave later services
    # unprovisioned — exactly the worktree-start partial-boot class.
    def test_prepare_all_is_best_effort_across_services(self) -> None:
        overlay = _FailingPrereqOverlay(self.order_file)
        ServiceLauncher.prepare_all(self.worktree, list(FullStackOverlay.SERVICES), overlay=overlay)
        ran = sorted(self.order_file.read_text().splitlines())
        assert ran == ["prereq-backend", "prereq-frontend", "prereq-microservice"]


class WorktreeProvisionRunnerContractTests(TestCase):
    """The real provision runner against a real tmp worktree.

    Only seams mocked: the direnv/prek subprocess (`run_step`) and which
    overlay the env-cache renderer loads. DB import is absent (default
    strategy None). Everything else — env-cache write, step ordering,
    required-failure propagation — runs for real.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3")
        self._tmp = tempfile.TemporaryDirectory()
        self.order_file = Path(self._tmp.name) / "order.txt"
        self.worktree = _provisioned_worktree(self._tmp.name, self.ticket)
        self._run_step = patch(
            "teatree.core.runners.worktree_provision.run_step",
            return_value=StepResult(name="stub", success=True),
        )
        self._run_step.start()

    def tearDown(self) -> None:
        self._run_step.stop()
        self._tmp.cleanup()

    def _run(self, overlay: FullStackOverlay) -> object:
        # The runner threads its resolved overlay straight into the env-cache
        # writer (souliane/teatree#1975), so no overlay-loader patch is needed.
        return WorktreeProvisionRunner(self.worktree, overlay=overlay).run()

    # Provision steps run before per-service pre-run steps, in declared
    # order — the env cache and overlay schema must exist before anything
    # that depends on them.
    def test_provision_steps_run_before_pre_run_in_order(self) -> None:
        overlay = FullStackOverlay(self.order_file)
        result = self._run(overlay)
        lines = self.order_file.read_text().splitlines()
        assert lines[:2] == ["prov-schema", "prov-seed"]
        assert sorted(lines[2:]) == ["prereq-backend", "prereq-frontend", "prereq-microservice"]
        assert result.ok

    # A required provision step failing aborts the provision phase (halts
    # before later required steps) and the runner reports not-ok naming the
    # failed step — the worker uses this to NOT advance the FSM, so a half
    # -provisioned worktree never looks "provisioned".
    def test_required_provision_failure_halts_and_marks_not_ok(self) -> None:
        overlay = FullStackOverlay(self.order_file, fail_provision_step="schema")
        result = self._run(overlay)
        lines = self.order_file.read_text().splitlines()
        assert "prov-schema" in lines
        assert "prov-seed" not in lines  # required-failure halted before it
        assert not result.ok
        assert "schema" in result.detail

    # souliane/teatree#2949 — the ProvisionReport is persisted to
    # Worktree.extra so `--report` / worktree status can render it later,
    # with no schema change (extra is existing JSON).
    def test_successful_provision_persists_report_to_worktree_extra(self) -> None:
        overlay = FullStackOverlay(self.order_file)
        result = self._run(overlay)
        assert result.ok

        self.worktree.refresh_from_db()
        report_data = self.worktree.extra["provision_report"]
        assert report_data["success"] is True
        step_names = {s["name"] for s in report_data["steps"]}
        assert {"prov-schema", "prov-seed", "prereq-backend", "prereq-frontend", "prereq-microservice"} == step_names
        assert report_data["total_duration"] >= 0

    def test_failed_provision_still_persists_report(self) -> None:
        overlay = FullStackOverlay(self.order_file, fail_provision_step="schema")
        result = self._run(overlay)
        assert not result.ok

        self.worktree.refresh_from_db()
        report_data = self.worktree.extra["provision_report"]
        assert report_data["success"] is False
        step_names = [s["name"] for s in report_data["steps"]]
        # halted before "prov-seed" (required-failure), but the post-db/pre-run
        # phases still run unconditionally (best-effort, pre-existing behavior).
        assert "prov-schema" in step_names
        assert "prov-seed" not in step_names

    def test_slow_provision_fires_out_of_band_alert(self) -> None:
        overlay = FullStackOverlay(self.order_file)
        with (
            patch("teatree.core.runners.worktree_provision.alert_provision_user") as mock_alert,
            patch("teatree.core.runners.worktree_provision.get_effective_settings") as mock_settings,
        ):
            mock_settings.return_value = SimpleNamespace(provision_slow_threshold_seconds=-1)
            result = self._run(overlay)
        assert result.ok
        mock_alert.assert_called_once()
        assert mock_alert.call_args.kwargs["step"] == "provision"

    def test_fast_provision_does_not_fire_alert(self) -> None:
        overlay = FullStackOverlay(self.order_file)
        with patch("teatree.core.runners.worktree_provision.alert_provision_user") as mock_alert:
            result = self._run(overlay)
        assert result.ok
        mock_alert.assert_not_called()


class WorktreeStartRunnerContractTests(TestCase):
    """The real start runner: prepare_all must run before the compose phase.

    Only seams mocked: docker_compose_down (subprocess boundary) and the
    env-cache overlay loader. With no compose file the runner returns ok
    right after prepare_all + env-cache write, which is exactly the window
    we assert: every service's prereqs ran before any compose work.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4")
        self._tmp = tempfile.TemporaryDirectory()
        self.order_file = Path(self._tmp.name) / "order.txt"
        self.worktree = _provisioned_worktree(self._tmp.name, self.ticket)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_prepare_all_runs_before_compose_phase(self) -> None:
        overlay = FullStackOverlay(self.order_file)
        with patch("teatree.core.runners.worktree_start.docker_compose_down") as down:
            result = WorktreeStartRunner(self.worktree, overlay=overlay).run()
        down.assert_called_once()
        ran = sorted(self.order_file.read_text().splitlines())
        assert ran == ["prereq-backend", "prereq-frontend", "prereq-microservice"]
        assert result.ok
        assert "no compose file" in result.detail


class WorktreeFsmTransitionTests(TestCase):
    """Every legal lifecycle transition, and illegal ones rejected.

    Pure django_fsm model behavior — no mocks; on_commit task enqueues
    never fire inside the test transaction.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/5")
        self.worktree = Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b")

    def _advance(self, fn: Callable[[], None], expected: str) -> None:
        fn()
        self.worktree.save()
        self.worktree.refresh_from_db()
        assert self.worktree.state == expected, f"→ {self.worktree.state}, want {expected}"

    def test_full_legal_lifecycle_path(self) -> None:
        assert self.worktree.state == Worktree.State.CREATED
        self._advance(self.worktree.provision, Worktree.State.PROVISIONED)
        self._advance(self.worktree.start_services, Worktree.State.SERVICES_UP)
        self._advance(self.worktree.verify, Worktree.State.READY)
        self._advance(self.worktree.db_refresh, Worktree.State.PROVISIONED)
        self._advance(self.worktree.teardown, Worktree.State.CREATED)

    def test_illegal_transitions_are_rejected(self) -> None:
        # Cannot start services or verify straight from CREATED.
        with pytest.raises(TransitionNotAllowed):
            self.worktree.start_services()
        with pytest.raises(TransitionNotAllowed):
            self.worktree.verify()
        # verify requires a running stack, not merely PROVISIONED.
        self.worktree.provision()
        self.worktree.save()
        with pytest.raises(TransitionNotAllowed):
            self.worktree.verify()


class _FixedExtrasOverlay(CommandOverlay):
    def get_e2e_env_extras(self, env_cache: dict[str, str]) -> dict[str, str]:
        return {"E2E_CONTRACT_KEY": "from-overlay", "E2E_ONLY_OVERLAY": "filled"}


class E2eEnvMergeContractTests(TestCase):
    """The setdefault merge in _build_e2e_env.

    An explicit environment value always wins over an overlay extra (the
    #121-class regression where a DEV credential got clobbered by the
    local default), while overlay extras still fill keys the environment
    doesn't set.
    """

    def _build(self) -> dict[str, str]:
        with (
            patch("teatree.core.management.commands._e2e_runners.get_overlay", return_value=_FixedExtrasOverlay()),
            patch("teatree.core.management.commands._e2e_runners._find_env_cache", return_value=None),
        ):
            return _build_e2e_env(None, headed=False, target="local")

    def test_explicit_env_wins_over_overlay_extra(self) -> None:
        with patch.dict(os.environ, {"E2E_CONTRACT_KEY": "from-env"}, clear=False):
            env = self._build()
        assert env["E2E_CONTRACT_KEY"] == "from-env"

    def test_overlay_extra_fills_absent_key(self) -> None:
        os.environ.pop("E2E_ONLY_OVERLAY", None)
        env = self._build()
        assert env["E2E_ONLY_OVERLAY"] == "filled"
