"""``worktree start`` destroys LAST — every fallible step runs before the down.

The incident this pins: the runner brought the compose project DOWN as its first
act and only then resolved run commands, ran pre-run steps and wrote the env
cache. A failure in any of those left the operator with no stack and no path
back, because ``docker compose up`` is itself gated. A destructive step that
runs before its own preconditions are validated is wrong regardless of what
trips it, so the tests below assert the ORDER, not the trigger.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import (
    OverlayBase,
    OverlayProvisioning,
    OverlayReview,
    OverlayRuntime,
    ProvisionStep,
    RunCommands,
)
from teatree.core.runners import worktree_start as start_mod
from teatree.core.runners.worktree_start import WorktreeStartRunner


class _StackProvisioning(OverlayProvisioning):
    def __init__(self, compose_file: str) -> None:
        self._compose_file = compose_file

    def compose_file(self, worktree: Worktree) -> str:
        _ = worktree
        return self._compose_file


class _StackRuntime(OverlayRuntime):
    def run_commands(self, worktree: Worktree) -> RunCommands:
        return {"backend": ["run-backend", worktree.repo_path]}


class _StackReview(OverlayReview):
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return False


class _StackOverlay(OverlayBase):
    """Overlay double that declares one service and one compose file."""

    review = _StackReview()
    runtime = _StackRuntime()

    def __init__(self, compose_file: str) -> None:
        self.provisioning = _StackProvisioning(compose_file)

    def get_repos(self) -> list[str]:
        return ["repo"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        _ = worktree
        return []


class _StartRunnerTest(TestCase):
    """A provisioned worktree whose recorded path exists in THIS venue."""

    @pytest.fixture(autouse=True)
    def _worktree_on_disk(self, tmp_path: Path) -> None:
        self.wt_path = tmp_path / "ticket" / "repo"
        self.wt_path.mkdir(parents=True)
        self.compose_file = self.wt_path / "docker-compose.yml"
        self.compose_file.write_text("services: {}\n", encoding="utf-8")

    def _worktree(self) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/42")
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="repo",
            branch="42-feat",
            state=Worktree.State.SERVICES_UP,
            db_name="wt_42",
            compose_project="repo-wt42",
            extra={"worktree_path": str(self.wt_path)},
        )

    def _overlay(self) -> _StackOverlay:
        return _StackOverlay(str(self.compose_file))


class TestNothingDestructiveRunsBeforePreparationSucceeds(_StartRunnerTest):
    def test_env_cache_failure_leaves_the_running_stack_untouched(self) -> None:
        worktree = self._worktree()
        with (
            patch.object(start_mod, "docker_compose_down") as down,
            patch.object(start_mod, "write_env_cache", side_effect=PermissionError("Permission denied: '/Users'")),
            pytest.raises(PermissionError),
        ):
            WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        down.assert_not_called()

    def test_pre_run_step_failure_leaves_the_running_stack_untouched(self) -> None:
        worktree = self._worktree()
        with (
            patch.object(start_mod, "docker_compose_down") as down,
            patch.object(start_mod.ServiceLauncher, "prepare_all", side_effect=RuntimeError("pre-run step blew up")),
            pytest.raises(RuntimeError),
        ):
            WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        down.assert_not_called()

    def test_image_build_failure_leaves_the_running_stack_untouched(self) -> None:
        worktree = self._worktree()
        with (
            patch.object(start_mod, "docker_compose_down") as down,
            patch.object(start_mod, "write_env_cache"),
            patch.object(WorktreeStartRunner, "_ensure_images_built", return_value="image build failed"),
        ):
            result = WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        assert not result.ok
        assert "image build failed" in result.detail
        down.assert_not_called()

    def test_down_runs_immediately_before_up_once_preparation_succeeded(self) -> None:
        worktree = self._worktree()
        calls: list[str] = []
        with (
            patch.object(start_mod, "write_env_cache", side_effect=lambda *a, **k: calls.append("env_cache")),
            patch.object(
                start_mod.ServiceLauncher, "prepare_all", side_effect=lambda *a, **k: calls.append("prepare_all")
            ),
            patch.object(start_mod, "docker_compose_down", side_effect=lambda *a, **k: calls.append("down")),
            patch.object(
                WorktreeStartRunner, "_ensure_images_built", side_effect=lambda *a, **k: calls.append("build") or None
            ),
            patch.object(
                WorktreeStartRunner, "_compose_up", side_effect=lambda *a, **k: calls.append("up") or (True, "")
            ),
            patch.object(start_mod, "get_worktree_ports", return_value={}),
        ):
            result = WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        assert result.ok
        assert calls == ["prepare_all", "env_cache", "build", "down", "up"]


class TestUnreachableRecordedPathRefusesBeforeAnyDockerCall(_StartRunnerTest):
    """A path minted in another venue is refused, not crashed into (#3912's sibling)."""

    def _worktree_recorded_elsewhere(self) -> Worktree:
        worktree = self._worktree()
        worktree.extra = {"worktree_path": "/Users/someone/workspace/t3-workspaces/repo"}
        worktree.save(update_fields=["extra"])
        return worktree

    def test_refuses_without_downing_the_stack(self) -> None:
        worktree = self._worktree_recorded_elsewhere()
        with (
            patch.object(start_mod, "docker_compose_down") as down,
            patch.object(start_mod, "write_env_cache") as env_cache,
        ):
            result = WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        assert not result.ok
        down.assert_not_called()
        env_cache.assert_not_called()

    def test_names_the_venue_boundary_as_the_cause(self) -> None:
        worktree = self._worktree_recorded_elsewhere()
        with patch.object(start_mod, "docker_compose_down"):
            result = WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        assert "/Users/someone/workspace/t3-workspaces/repo" in result.detail
        assert "outside the container" in result.detail

    def test_a_genuinely_deleted_checkout_is_reported_as_deleted_not_as_a_venue_mismatch(self) -> None:
        worktree = self._worktree()
        worktree.extra = {"worktree_path": str(self.wt_path.parent / "gone")}
        worktree.save(update_fields=["extra"])
        with patch.object(start_mod, "docker_compose_down") as down:
            result = WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        assert not result.ok
        assert "no longer exists" in result.detail
        down.assert_not_called()


class TestFailedStartDoesNotLeaveAFalseServicesUp(_StartRunnerTest):
    """``services_up`` must mean containers are up — the row is reconciled, never assumed."""

    def test_demotes_to_provisioned_when_the_stack_is_provably_empty(self) -> None:
        worktree = self._worktree()
        with (
            patch.object(start_mod, "docker_compose_down"),
            patch.object(start_mod, "write_env_cache"),
            patch.object(WorktreeStartRunner, "_ensure_images_built", return_value=None),
            patch.object(WorktreeStartRunner, "_compose_up", return_value=(False, "exit 1: boom")),
            patch.object(start_mod, "running_container_ids", return_value=[]),
        ):
            result = WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        worktree.refresh_from_db()
        assert not result.ok
        assert worktree.state == Worktree.State.PROVISIONED

    def test_keeps_services_up_when_containers_are_still_running(self) -> None:
        worktree = self._worktree()
        with (
            patch.object(start_mod, "docker_compose_down"),
            patch.object(start_mod, "write_env_cache"),
            patch.object(WorktreeStartRunner, "_ensure_images_built", return_value=None),
            patch.object(WorktreeStartRunner, "_compose_up", return_value=(False, "exit 1: one service failed")),
            patch.object(start_mod, "running_container_ids", return_value=["abc123"]),
        ):
            WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        worktree.refresh_from_db()
        assert worktree.state == Worktree.State.SERVICES_UP

    def test_keeps_services_up_when_docker_cannot_answer(self) -> None:
        worktree = self._worktree()
        with (
            patch.object(start_mod, "docker_compose_down"),
            patch.object(start_mod, "write_env_cache"),
            patch.object(WorktreeStartRunner, "_ensure_images_built", return_value=None),
            patch.object(WorktreeStartRunner, "_compose_up", return_value=(False, "exit 1: boom")),
            patch.object(start_mod, "running_container_ids", return_value=None),
        ):
            WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        worktree.refresh_from_db()
        assert worktree.state == Worktree.State.SERVICES_UP

    def test_reporting_a_failure_never_raises_on_a_row_that_moved_on(self) -> None:
        worktree = self._worktree()
        worktree.state = Worktree.State.PROVISIONED
        worktree.save(update_fields=["state"])
        with (
            patch.object(start_mod, "docker_compose_down"),
            patch.object(start_mod, "write_env_cache"),
            patch.object(WorktreeStartRunner, "_ensure_images_built", return_value=None),
            patch.object(WorktreeStartRunner, "_compose_up", return_value=(False, "exit 1: boom")),
            patch.object(start_mod, "running_container_ids", return_value=[]),
        ):
            result = WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        assert not result.ok
        assert "boom" in result.detail

    def test_a_refused_start_leaves_the_recorded_state_alone(self) -> None:
        """The venue refusal touched nothing, so the row still describes the live stack."""
        worktree = self._worktree()
        worktree.extra = {"worktree_path": "/Users/someone/workspace/t3-workspaces/repo"}
        worktree.save(update_fields=["extra"])
        with patch.object(start_mod, "running_container_ids") as probe:
            WorktreeStartRunner(worktree, overlay=self._overlay()).run()
        worktree.refresh_from_db()
        assert worktree.state == Worktree.State.SERVICES_UP
        probe.assert_not_called()
