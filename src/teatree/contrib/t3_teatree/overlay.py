"""Bundled overlay for teatree self-development (dogfooding).

Provides a real overlay that exercises the full overlay API using
teatree's own repo, skills, and GitHub project as the target.
"""

from pathlib import Path
from typing import override

from teatree.config import discover_overlays, load_config
from teatree.core.models import Worktree
from teatree.core.overlay import OverlayBase, OverlayConfig, OverlayMetadata
from teatree.types import ProvisionStep, RunCommands, SkillMetadata
from teatree.utils.run import run_checked
from teatree.visual_qa import matches_triggers

_SETTINGS_MODULE = "teatree.contrib.t3_teatree.overlay_settings"


def _repo_root() -> Path:
    """Return the teatree repository root (directory containing pyproject.toml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "skills").is_dir():
            return parent
    msg = f"Cannot find teatree repo root from {here}"
    raise FileNotFoundError(msg)


def _discover_workspace_repos() -> list[str]:
    """Aggregate teatree's own repo + every discovered overlay project path.

    Each path is returned relative to ``workspace_dir``. Overlays whose path
    lives outside ``workspace_dir`` (or cannot be resolved on disk) are
    skipped — callers can always override via ``config.workspace_repos``.
    """
    workspace_dir = load_config().user.workspace_dir.resolve()
    candidates: list[Path] = [_repo_root()]
    candidates.extend(entry.project_path for entry in discover_overlays() if entry.project_path is not None)

    seen: set[str] = set()
    repos: list[str] = []
    for candidate in candidates:
        try:
            rel = candidate.resolve().relative_to(workspace_dir)
        except ValueError:
            continue
        key = str(rel)
        if key not in seen:
            seen.add(key)
            repos.append(key)
    return repos


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

    @override
    def get_e2e_config(self) -> dict[str, str]:
        return {
            "runner": "project",
            "test_dir": "e2e/",
            "settings_module": "e2e.settings",
        }


class TeatreeOverlay(OverlayBase):
    """Overlay for developing teatree itself."""

    django_app: str | None = "teatree.contrib.t3_teatree"
    config = OverlayConfig(settings_module=_SETTINGS_MODULE, overlay_name="teatree")
    metadata = TeatreeMetadata()

    @override
    def get_repos(self) -> list[str]:
        return ["teatree"]

    @override
    def get_workspace_repos(self) -> list[str]:
        if self.config.workspace_repos:
            return list(self.config.workspace_repos)
        discovered = _discover_workspace_repos()
        return discovered or self.get_repos()

    @override
    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        repo = Path(worktree.repo_path)

        def sync_deps() -> None:
            run_checked(["uv", "sync"], cwd=repo)

        def install_overlays_editable() -> None:
            workspace_dir = load_config().user.workspace_dir.resolve()
            ticket_dir = repo.parent
            repo_resolved = repo.resolve()
            for entry in discover_overlays():
                if entry.project_path is None:
                    continue
                try:
                    entry.project_path.resolve().relative_to(workspace_dir)
                except ValueError:
                    continue
                overlay_worktree = ticket_dir / entry.project_path.name
                if not overlay_worktree.is_dir():
                    continue
                if overlay_worktree.resolve() == repo_resolved:
                    continue
                run_checked(["uv", "pip", "install", "-e", str(overlay_worktree)], cwd=repo)

        return [
            ProvisionStep(
                name="sync-dependencies",
                callable=sync_deps,
                description="Install Python dependencies with uv sync",
            ),
            ProvisionStep(
                name="install-overlays-editable",
                callable=install_overlays_editable,
                description="Install discovered overlays editable from their ticket worktrees",
            ),
        ]

    @override
    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "test": ["uv", "run", "pytest"],
            "lint": ["prek", "run", "--all-files"],
        }

    @override
    def get_test_command(self, worktree: Worktree) -> list[str]:
        return ["uv", "run", "pytest"]

    @override
    def get_visual_qa_targets(self, changed_files: list[str]) -> list[str]:
        teatree_globs = (
            "src/teatree/**/templates/**",
            "src/teatree/**/static/**",
            "src/teatree/core/views/**",
            "src/teatree/core/urls.py",
        )
        return ["/"] if matches_triggers(changed_files, teatree_globs) else []
