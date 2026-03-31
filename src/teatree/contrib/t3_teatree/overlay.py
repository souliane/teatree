"""Bundled overlay for teatree self-development (dogfooding).

Provides a real overlay that exercises the full overlay API using
teatree's own repo, skills, and GitHub project as the target.
"""

from pathlib import Path
from typing import override

from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase, OverlayMetadata, ProvisionStep, SkillMetadata


def _repo_root() -> Path:
    """Return the teatree repository root (directory containing pyproject.toml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "skills").is_dir():
            return parent
    msg = f"Cannot find teatree repo root from {here}"
    raise FileNotFoundError(msg)


class TeatreeMetadata(OverlayMetadata):
    """Metadata for the bundled teatree overlay."""

    @override
    def get_followup_repos(self) -> list[str]:
        return ["souliane/teatree"]

    @override
    def get_skill_metadata(self) -> SkillMetadata:
        root = _repo_root()
        return {
            "skill_path": str(root / "skills"),
            "remote_patterns": ["souliane/teatree"],
        }


class TeatreeOverlay(OverlayBase):
    """Overlay for developing teatree itself."""

    django_app: str | None = "teatree.contrib.t3_teatree"
    metadata = TeatreeMetadata()

    @override
    def get_repos(self) -> list[str]:
        return ["teatree"]

    @override
    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        repo = Path(worktree.repo_path)

        def sync_deps() -> None:
            import subprocess  # noqa: PLC0415, S404

            subprocess.run(["uv", "sync"], cwd=repo, check=True)  # noqa: S607

        return [
            ProvisionStep(
                name="sync-dependencies",
                callable=sync_deps,
                description="Install Python dependencies with uv sync",
            ),
        ]

    @override
    def get_run_commands(self, worktree: Worktree) -> dict[str, list[str]]:
        return {
            "test": ["uv", "run", "pytest"],
            "lint": ["prek", "run", "--all-files"],
        }

    @override
    def get_test_command(self, worktree: Worktree) -> list[str]:
        return ["uv", "run", "pytest"]

    @override
    def get_workspace_repos(self) -> list[str]:
        return ["teatree"]
