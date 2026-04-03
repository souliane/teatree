from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from teatree.core.models import Worktree


@dataclass(frozen=True)
class RunCommand:
    """Structured run command with explicit working directory."""

    args: list[str] = field(default_factory=list)
    cwd: Path | None = None


type RunCommands = dict[str, list[str] | RunCommand]


class SymlinkSpec(TypedDict, total=False):
    path: str
    source: str
    mode: str
    description: str


class ServiceSpec(TypedDict, total=False):
    shared: bool
    service: str
    compose_file: str
    start_command: list[str]
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
    teatree_version: str


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

    Configure via an ``overlay_settings`` module (Django-style) referenced by
    the overlay class, or by subclassing and setting attributes directly.

    Settings modules use ``UPPER_CASE`` constants that map to ``lower_case``
    attributes on this class.  Settings ending in ``_PASS_KEY`` become secret
    readers: ``GITHUB_TOKEN_PASS_KEY = "github/token"`` makes
    ``get_github_token()`` read from the ``pass`` password store.
    """

    # ── Static settings (override via settings module or subclass) ───

    gitlab_url: str = "https://gitlab.com/api/v4"
    github_owner: str = ""
    """GitHub user or org that owns the project board."""
    github_project_number: int = 0
    """GitHub Projects v2 board number (0 = not configured)."""
    require_ticket: bool = False
    """Whether to enforce a tracked issue before coding/shipping."""
    known_variants: list[str]
    mr_auto_labels: list[str]
    frontend_repos: list[str]
    dev_env_url: str = ""
    dashboard_logo: str = ""

    def __init__(self, settings_module: str = "", overlay_name: str = "") -> None:
        # Initialize mutable defaults per-instance
        self.known_variants = []
        self.mr_auto_labels = []
        self.frontend_repos = []
        if settings_module:
            self._load_settings(settings_module)
        if overlay_name:
            self._load_toml_overrides(overlay_name)

    def _load_settings(self, module_path: str) -> None:
        """Load UPPER_CASE constants from a settings module as attributes.

        ``*_PASS_KEY`` settings register a secret reader via ``pass``.
        """
        mod = import_module(module_path)
        for name in dir(mod):
            if not name.isupper() or name.startswith("_"):
                continue
            value = getattr(mod, name)
            if name.endswith("_PASS_KEY"):
                # GITHUB_TOKEN_PASS_KEY → get_github_token() reads from pass
                attr_name = name.removesuffix("_PASS_KEY").lower()
                self._register_secret(attr_name, str(value))
            else:
                setattr(self, name.lower(), value)

    def _load_toml_overrides(self, overlay_name: str) -> None:
        """Load overlay-specific overrides from ``~/.teatree.toml``.

        Reads ``[overlays.<name>]`` section. Keys ending in ``_pass_key``
        register secret readers, others set attributes directly.
        """
        from teatree.config import load_config  # noqa: PLC0415

        config = load_config()
        overlay_cfg = config.raw.get("overlays", {}).get(overlay_name, {})
        for key, value in overlay_cfg.items():
            if key in {"class", "path"}:
                continue  # reserved keys for overlay discovery
            if key.endswith("_pass_key"):
                attr_name = key.removesuffix("_pass_key")
                self._register_secret(attr_name, str(value))
            else:
                setattr(self, key, value)

    def _register_secret(self, attr_name: str, pass_key: str) -> None:
        """Create a ``get_<attr_name>()`` method that reads from ``pass``."""

        def _reader(_key: str = pass_key) -> str:
            from teatree.utils.secrets import read_pass  # noqa: PLC0415

            return read_pass(_key)

        method_name = f"get_{attr_name}"
        # Bind to the instance (not the class) so other OverlayConfig
        # instances are unaffected — prevents test pollution.
        setattr(self, method_name, _reader)

    # ── Secret getters (override in subclass or via *_PASS_KEY) ──────

    def get_gitlab_token(self) -> str:
        return ""

    def get_gitlab_username(self) -> str:
        return ""

    def get_github_token(self) -> str:
        return ""

    def get_slack_token(self) -> str:
        return ""

    # ── Structured getters (need logic, can't be plain constants) ────

    def get_review_channel(self) -> tuple[str, str]:
        """Return (channel_name, channel_id) for review notifications."""
        return ("", "")

    # ── Deprecated method accessors (use attributes instead) ─────────
    # Kept for backwards compatibility with existing overlay code.

    def get_gitlab_url(self) -> str:
        return self.gitlab_url

    def get_known_variants(self) -> list[str]:
        return self.known_variants

    def get_mr_auto_labels(self) -> list[str]:
        return self.mr_auto_labels

    def get_frontend_repos(self) -> list[str]:
        return self.frontend_repos

    def get_dev_env_url(self) -> str:
        return self.dev_env_url

    def get_dashboard_logo(self) -> str:
        return self.dashboard_logo


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

    def db_import(self, worktree: "Worktree", *, force: bool = False, slow_import: bool = False) -> bool:
        return False

    def get_post_db_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        return []

    def get_reset_passwords_command(self, worktree: "Worktree") -> ProvisionStep | None:
        return None

    def get_envrc_lines(self, worktree: "Worktree") -> list[str]:
        return []

    def get_symlinks(self, worktree: "Worktree") -> list[SymlinkSpec]:
        return []

    def get_services_config(self, worktree: "Worktree") -> dict[str, ServiceSpec]:
        return {}

    def get_compose_file(self, worktree: "Worktree") -> str:
        """Return the path to the docker-compose file for this worktree."""
        return ""

    # ── Run hooks ────────────────────────────────────────────────────

    def get_run_commands(self, worktree: "Worktree") -> RunCommands:
        return {}

    def get_pre_run_steps(self, worktree: "Worktree", service: str) -> list[ProvisionStep]:
        return []

    def get_test_command(self, worktree: "Worktree") -> list[str]:
        return []

    def get_verify_endpoints(self, worktree: "Worktree") -> dict[str, str]:
        """Return custom health-check paths per service.

        Keys match ``worktree.ports`` entries (e.g. ``"backend"``, ``"frontend"``).
        Values are URL paths (e.g. ``"/admin/login/"``).
        Services not listed here fall back to ``/``.
        """
        return {}

    def get_timeouts(self) -> dict[str, int]:
        """Return overlay-specific timeout overrides (seconds).

        Keys match ``teatree.timeouts`` operation names (e.g. ``"setup"``,
        ``"db_import"``).  ``0`` disables the timeout for that operation.
        Only return overrides — missing keys fall through to core defaults.
        """
        return {}

    def get_cleanup_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        """Return extra cleanup steps run before a worktree is removed.

        Use for overlay-specific teardown (Docker containers, cache dirs, etc.).
        """
        return []

    def get_health_checks(self, worktree: "Worktree") -> list["HealthCheck"]:
        """Return post-provision health checks to verify the worktree is functional.

        Overlays can override to add project-specific checks (e.g., verify
        specific DB tables exist, check custom symlinks).  The default checks
        verify: worktree path exists, symlinks are valid, and DB name is set.
        """
        return _default_health_checks(self, worktree)

    def get_workspace_repos(self) -> list[str]:
        return self.get_repos()


# ── Health checks ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    check: Callable[[], bool]
    description: str = ""


def _default_health_checks(overlay: OverlayBase, worktree: "Worktree") -> list[HealthCheck]:
    """Return standard post-provision checks applicable to any overlay."""
    checks: list[HealthCheck] = []
    extra = worktree.extra or {}
    wt_path = extra.get("worktree_path", "")

    if wt_path:
        checks.append(
            HealthCheck(
                name="worktree-exists",
                check=lambda: Path(wt_path).is_dir(),
                description=f"Worktree directory exists: {wt_path}",
            )
        )

        # Verify symlinks point to valid targets
        for spec in overlay.get_symlinks(worktree):
            dest = Path(wt_path) / spec.get("path", "")
            source = Path(spec.get("source", ""))
            if spec.get("mode", "symlink") == "symlink" and source.exists():
                checks.append(
                    HealthCheck(
                        name=f"symlink-{spec.get('path', '?')}",
                        check=lambda d=dest: d.exists() or d.is_symlink(),
                        description=f"Symlink exists: {spec.get('path', '')}",
                    )
                )

    if worktree.db_name:
        checks.append(
            HealthCheck(
                name="db-name-set",
                check=lambda: bool(worktree.db_name),
                description=f"Database name assigned: {worktree.db_name}",
            )
        )

    return checks
