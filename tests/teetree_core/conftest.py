"""Shared fixtures for teetree.core test modules."""

from collections.abc import Iterator
from typing import cast

import pytest

from teetree.core.models import Worktree
from teetree.core.overlay import OverlayBase, ProvisionStep, RunCommands
from teetree.core.overlay_loader import reset_overlay_cache

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
            "backend": f"run-backend {worktree.repo_path}",
            "frontend": f"run-frontend {worktree.repo_path}",
        }


COMMAND_OVERLAY = "tests.teetree_core.conftest.CommandOverlay"

COMMAND_SETTINGS = {
    "TEATREE_OVERLAY_CLASS": COMMAND_OVERLAY,
    "TEATREE_HEADLESS_RUNTIME": "claude-code",
    "TEATREE_INTERACTIVE_RUNTIME": "codex",
    "TEATREE_TERMINAL_MODE": "same-terminal",
}


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()
