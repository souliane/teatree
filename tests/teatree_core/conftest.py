"""Shared fixtures for teatree.core test modules."""

from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

import pytest

from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep, RunCommands
from teatree.core.overlay_loader import reset_overlay_cache

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class CommandOverlay(OverlayBase):
    """Minimal overlay for management command tests."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        def remember_setup() -> None:
            extra = cast("dict[str, str]", worktree.extra or {})
            extra["setup_hook"] = "ran"
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name="remember-setup", callable=remember_setup)]

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": ["run-backend", worktree.repo_path],
            "frontend": ["run-frontend", worktree.repo_path],
        }

    def get_pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def remember_pre_run() -> None:
            extra = cast("dict[str, str]", worktree.extra or {})
            extra[f"pre_run_{service}"] = "ran"
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name=f"pre-run-{service}", callable=remember_pre_run)]


COMMAND_OVERLAY = "tests.teatree_core.conftest.CommandOverlay"

COMMAND_SETTINGS = {
    "TEATREE_TERMINAL_MODE": "same-terminal",
}


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


@pytest.fixture(autouse=True)
def _mock_redis_container() -> Iterator[None]:
    """Prevent tests from shelling out to docker for the shared Redis container."""
    with patch("teatree.utils.redis_container.ensure_running"):
        yield


@pytest.fixture
def mock_command_overlay() -> Iterator[None]:
    """Patch _discover_overlays to return a CommandOverlay instance."""
    with patch(
        "teatree.core.overlay_loader._discover_overlays",
        return_value={"test": CommandOverlay()},
    ):
        yield
