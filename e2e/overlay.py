"""Dummy overlay for E2E testing."""

from teatree.core.models import Worktree
from teatree.core.overlay import (
    OverlayBase,
    ProvisionStep,
    RunCommands,
    SkillMetadata,
)


class E2EOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["demo-backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {"backend": ["echo", "running"], "frontend": ["echo", "running"]}

    def get_skill_metadata(self) -> SkillMetadata:
        return {"skill_path": "e2e/SKILL.md"}
