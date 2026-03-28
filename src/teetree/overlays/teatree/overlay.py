from pathlib import Path
from typing import TYPE_CHECKING, override

from teetree.core.overlay import OverlayBase, ProvisionStep, SkillMetadata

if TYPE_CHECKING:
    from teetree.core.models import Worktree


class TeaTreeOverlay(OverlayBase):
    @override
    def get_repos(self) -> list[str]:
        return ["teatree"]

    @override
    def get_provision_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        _ = worktree
        return [
            ProvisionStep(
                name="install-deps",
                callable=lambda: None,
                description="Install Python dependencies with uv sync",
            ),
        ]

    @override
    def get_test_command(self, worktree: "Worktree") -> str:
        _ = worktree
        return "uv run pytest"

    @override
    def get_skill_metadata(self) -> SkillMetadata:
        skill_dir = Path(__file__).resolve().parents[3] / "skills"
        return {
            "skill_path": str(skill_dir),
        }

    @override
    def get_run_commands(self, worktree: "Worktree") -> dict[str, str]:
        _ = worktree
        return {
            "docs": "uv run mkdocs serve",
        }
