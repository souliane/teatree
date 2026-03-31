from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from teatree.core.models import Worktree

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
    remote_patterns: list[str]
    trigger_index: list[dict[str, object]]


class ToolCommand(TypedDict, total=False):
    name: str
    help: str
    command: str
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


# ── Overlay configuration ────────────────────────────────────────────


class OverlayConfig:
    """Overlay-specific configuration — credentials, project settings, URLs.

    Subclass and assign to ``OverlayBase.config`` to provide project-specific
    values.  Consumers access config via ``overlay.config.get_gitlab_token()``.
    """

    def get_gitlab_token(self) -> str:
        return ""

    def get_gitlab_url(self) -> str:
        return "https://gitlab.com/api/v4"

    def get_gitlab_username(self) -> str:
        return ""

    def get_slack_token(self) -> str:
        return ""

    def get_review_channel(self) -> tuple[str, str]:
        """Return (channel_name, channel_id) for review notifications."""
        return ("", "")

    def get_known_variants(self) -> list[str]:
        return []

    def get_mr_auto_labels(self) -> list[str]:
        return []

    def get_frontend_repos(self) -> list[str]:
        return []

    def get_dev_env_url(self) -> str:
        return ""

    def get_dashboard_logo(self) -> str:
        return ""


# ── Overlay metadata ─────────────────────────────────────────────────


class OverlayMetadata:
    """Project metadata, CI integration, MR validation, and skill registration.

    Subclass and assign to ``OverlayBase.metadata`` for project-specific values.
    Consumers access via ``overlay.metadata.get_skill_metadata()``.
    """

    def validate_mr(self, title: str, description: str) -> ValidationResult:
        return {"errors": [], "warnings": []}

    def get_followup_repos(self) -> list[str]:
        return []

    def get_skill_metadata(self) -> SkillMetadata:
        return {}

    def get_ci_project_path(self) -> str:
        return ""

    def get_e2e_config(self) -> dict[str, str]:
        return {}

    def detect_variant(self) -> str:
        return ""

    def get_tool_commands(self) -> list[ToolCommand]:
        return []


# ── Overlay base class ───────────────────────────────────────────────


class OverlayBase(ABC):
    django_app: str | None = None
    config: OverlayConfig = OverlayConfig()
    metadata: OverlayMetadata = OverlayMetadata()

    # ── Required hooks ───────────────────────────────────────────────

    @abstractmethod
    def get_repos(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def get_provision_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        raise NotImplementedError

    # ── Provisioning hooks ───────────────────────────────────────────

    def get_env_extra(self, worktree: "Worktree") -> dict[str, str]:
        return {}

    def get_db_import_strategy(self, worktree: "Worktree") -> DbImportStrategy | None:
        return None

    def db_import(self, worktree: "Worktree", *, force: bool = False) -> bool:
        return False

    def get_post_db_steps(self, worktree: "Worktree") -> list[PostDbStep]:
        return []

    def get_reset_passwords_command(self, worktree: "Worktree") -> str:
        return ""

    def get_envrc_lines(self, worktree: "Worktree") -> list[str]:
        return []

    def get_symlinks(self, worktree: "Worktree") -> list[SymlinkSpec]:
        return []

    def get_services_config(self, worktree: "Worktree") -> dict[str, ServiceSpec]:
        return {}

    # ── Run hooks ────────────────────────────────────────────────────

    def get_run_commands(self, worktree: "Worktree") -> RunCommands:
        return {}

    def get_pre_run_steps(self, worktree: "Worktree", service: str) -> list[ProvisionStep]:
        return []

    def get_test_command(self, worktree: "Worktree") -> str:
        return ""

    def get_verify_endpoints(self, worktree: "Worktree") -> dict[str, str]:
        """Return custom health-check paths per service.

        Keys match ``worktree.ports`` entries (e.g. ``"backend"``, ``"frontend"``).
        Values are URL paths (e.g. ``"/admin/login/"``).
        Services not listed here fall back to ``/``.
        """
        return {}

    def get_cleanup_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        """Return extra cleanup steps run before a worktree is removed.

        Use for overlay-specific teardown (Docker containers, cache dirs, etc.).
        """
        return []

    def get_workspace_repos(self) -> list[str]:
        return self.get_repos()
