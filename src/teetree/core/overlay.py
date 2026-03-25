from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from teetree.core.models import Worktree

type RunCommands = dict[str, str]


class PostDbStep(TypedDict, total=False):
    name: str
    description: str
    command: str


class SymlinkSpec(TypedDict, total=False):
    path: str
    source: str
    mode: str
    description: str


class ServiceSpec(TypedDict, total=False):
    shared: bool
    service: str
    compose_file: str
    start_command: str
    readiness_check: str


class DbImportStrategy(TypedDict, total=False):
    kind: str
    source_database: str
    shared_postgres: bool
    snapshot_tool: str
    restore_order: list[str]
    notes: list[str]
    worktree_repo_path: str


class SkillMetadata(TypedDict, total=False):
    skill_path: str
    companion_skills: list[str]


class ToolCommand(TypedDict, total=False):
    name: str
    help: str
    management_command: str
    arguments: list[str]


class ValidationResult(TypedDict):
    errors: list[str]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class ProvisionStep:
    name: str
    callable: Callable[[], None]
    required: bool = True
    description: str = ""


class OverlayBase(ABC):
    @abstractmethod
    def get_repos(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def get_provision_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        raise NotImplementedError

    def get_env_extra(self, worktree: "Worktree") -> dict[str, str]:
        return {}

    def get_run_commands(self, worktree: "Worktree") -> RunCommands:
        return {}

    def get_test_command(self, worktree: "Worktree") -> str:
        """Return the shell command to run the project test suite."""
        return ""

    def get_db_import_strategy(self, worktree: "Worktree") -> DbImportStrategy | None:
        return None

    def db_import(self, worktree: "Worktree", *, force: bool = False) -> bool:
        """Import a database for the worktree. Return True on success."""
        return False

    def get_post_db_steps(self, worktree: "Worktree") -> list[PostDbStep]:
        return []

    def get_reset_passwords_command(self, worktree: "Worktree") -> str:
        """Return the shell command to reset all user passwords to a dev default."""
        return ""

    def get_envrc_lines(self, worktree: "Worktree") -> list[str]:
        """Return extra lines to append to .envrc in the worktree.

        Typical use: ``["[[ -f .venv/bin/activate ]] && source .venv/bin/activate"]``
        to auto-activate the Python venv when entering the worktree directory.
        """
        return []

    def get_symlinks(self, worktree: "Worktree") -> list[SymlinkSpec]:
        return []

    def get_services_config(self, worktree: "Worktree") -> dict[str, ServiceSpec]:
        return {}

    def validate_mr(self, title: str, description: str) -> ValidationResult:
        return {"errors": [], "warnings": []}

    def get_followup_repos(self) -> list[str]:
        """Return GitLab project paths (e.g. ``org/repo``) to sync MRs from."""
        return []

    def get_skill_metadata(self) -> SkillMetadata:
        return {}

    def get_ci_project_path(self) -> str:
        """Return the GitLab project path for CI operations (e.g. ``org/repo``)."""
        return ""

    def get_e2e_config(self) -> dict[str, str]:
        """Return E2E trigger configuration.

        Keys: ``project_path``, ``ref`` (branch/tag), ``variables`` (JSON).
        """
        return {}

    def detect_variant(self) -> str:
        """Detect the current tenant variant from environment."""
        return ""

    def get_workspace_repos(self) -> list[str]:
        """Return repo names for workspace ticket creation."""
        return self.get_repos()

    def get_tool_commands(self) -> list[ToolCommand]:
        """Return overlay-specific tool commands for ``t3 <overlay> tool``."""
        return []
