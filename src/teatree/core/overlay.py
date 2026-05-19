from abc import ABC, abstractmethod
from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING

from teatree.core.health import HealthCheck
from teatree.core.health import default_health_checks as _default_health_checks
from teatree.core.merge_guard import MergeGuard
from teatree.types import (
    BaseImageConfig,
    DbImportStrategy,
    ProvisionStep,
    RunCommand,
    RunCommands,
    ServiceSpec,
    SkillMetadata,
    SymlinkSpec,
    ToolCommand,
    ValidationResult,
)

if TYPE_CHECKING:
    from teatree.core.models import Worktree
    from teatree.core.readiness import Probe
    from teatree.types import RawAPIDict

# Re-export all types so existing ``from teatree.core.overlay import X`` still works.
__all__ = [
    "DEFAULT_TRANSITION_EMOJIS",
    "BaseImageConfig",
    "DbImportStrategy",
    "HealthCheck",
    "MergeGuard",
    "OverlayBase",
    "OverlayConfig",
    "OverlayMetadata",
    "ProvisionStep",
    "RunCommand",
    "RunCommands",
    "ServiceSpec",
    "SkillMetadata",
    "SymlinkSpec",
    "ToolCommand",
    "ValidationResult",
]


# ── Overlay configuration ────────────────────────────────────────────


DEFAULT_TRANSITION_EMOJIS: dict[str, str] = {
    "test": "white_check_mark",
    "request_review": "eyes",
    "approve": "white_check_mark",
    "mark_merged": "tada",
    "retrospect": "memo",
    "mark_delivered": "white_check_mark",
    "rework": "arrows_counterclockwise",
    "ignore": "wastebasket",
}


class OverlayConfig:
    # ── Static settings (override via settings module or subclass) ───

    gitlab_url: str = "https://gitlab.com/api/v4"
    github_owner: str = ""
    github_project_number: int = 0
    code_host: str = ""
    messaging_backend: str = "noop"
    slack_token_ref: str = ""
    # ``user_token_ref`` points at a ``pass`` entry holding the human user's
    # Slack OAuth token (``xoxp-…``).  Routed by ``SlackBotBackend`` for
    # reactions on Slack-Connect externally-shared channels where the bot
    # token is rejected by the workspace restriction policy.
    user_token_ref: str = ""
    slack_user_id: str = ""
    require_ticket: bool = False
    ready_labels: list[str]
    exclude_labels: list[str]
    auto_start_assigned_issues: bool = False
    max_concurrent_auto_starts: int = 1
    stale_threshold_days: int = 3
    notion_database_id: str = ""
    mr_close_ticket: bool = False
    teardown_removes_pass_entries: bool = False
    known_variants: list[str]
    pr_auto_labels: list[str]
    frontend_repos: list[str]
    workspace_repos: list[str]
    protected_branches: list[str]
    dev_env_url: str = ""

    def __init__(self, settings_module: str = "", overlay_name: str = "") -> None:
        # Initialize mutable defaults per-instance
        self.known_variants = []
        self.pr_auto_labels = []
        self.frontend_repos = []
        self.workspace_repos = []
        self.protected_branches = []
        self.ready_labels = []
        self.exclude_labels = []
        if settings_module:
            self._load_settings(settings_module)
        if overlay_name:
            self.apply_toml_overrides(overlay_name)

    def _load_settings(self, module_path: str) -> None:
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

    def apply_toml_overrides(self, overlay_name: str) -> None:
        """Apply ``[overlays.<overlay_name>]`` overrides from ``~/.teatree.toml``.

        Called automatically by ``__init__`` when an ``overlay_name`` is
        supplied, and by ``overlay_loader._discover_overlays`` for every
        entry-point overlay (so ``OverlayConfig`` subclasses don't have to
        opt in by threading ``overlay_name`` through every ``super().__init__``).
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
        return ("", "")

    def get_transition_emojis(self) -> dict[str, str]:
        override = getattr(self, "transition_emojis", None)
        if isinstance(override, dict):
            return {**DEFAULT_TRANSITION_EMOJIS, **override}
        return dict(DEFAULT_TRANSITION_EMOJIS)


# ── Overlay metadata ─────────────────────────────────────────────────


class OverlayMetadata:
    def validate_pr(self, title: str, description: str) -> ValidationResult:
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

    def get_issue_title(self, url: str) -> str:
        return ""


# ── Overlay base class ───────────────────────────────────────────────


class OverlayBase(ABC):  # noqa: PLR0904 — overlay extension API; hook count reflects surface, not poor encapsulation.
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

    # ── Issue title resolution ────────────────────────────────────────

    def get_issue_title(self, url: str) -> str:
        from teatree.backends.loader import get_code_host  # noqa: PLC0415

        try:
            host = get_code_host(self)
            if host is None:
                return ""
            data = host.get_issue(url)
            title = data.get("title", "") if isinstance(data, dict) else ""
            return str(title)
        except Exception:  # noqa: BLE001
            return ""

    # ── Provisioning hooks ───────────────────────────────────────────

    def get_env_extra(self, worktree: "Worktree") -> dict[str, str]:
        return {}

    def declared_env_keys(self) -> set[str]:
        return set()

    _CORE_SECRET_KEYS: frozenset[str] = frozenset({"POSTGRES_PASSWORD"})

    def declared_secret_env_keys(self) -> set[str]:
        return set(self._CORE_SECRET_KEYS)

    def get_db_import_strategy(self, worktree: "Worktree") -> DbImportStrategy | None:
        return None

    def db_import(  # noqa: PLR0913 — overlay extension-point contract; each kwarg is a documented hook input, not poor design.
        self,
        worktree: "Worktree",
        *,
        force: bool = False,
        slow_import: bool = False,
        dslr_snapshot: str = "",
        dump_path: str = "",
        approve_remote_dump: bool = False,
    ) -> bool:
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
        return ""

    def get_base_images(self, worktree: "Worktree") -> list[BaseImageConfig]:
        _ = worktree
        return []

    def get_docker_services(self, worktree: "Worktree") -> set[str]:
        _ = worktree
        return set()

    def uses_redis(self) -> bool:
        return False

    # ── Run hooks ────────────────────────────────────────────────────

    def get_run_commands(self, worktree: "Worktree") -> RunCommands:
        return {}

    def get_pre_run_steps(self, worktree: "Worktree", service: str) -> list[ProvisionStep]:
        return []

    def get_test_command(self, worktree: "Worktree") -> list[str] | RunCommand:
        return []

    def get_e2e_env_extras(self, env_cache: dict[str, str]) -> dict[str, str]:
        _ = env_cache
        return {}

    def get_e2e_preflight(self, *, customer: str | None, base_url: str | None) -> list[Callable[[], None]]:
        _ = customer, base_url
        return []

    def get_verify_endpoints(self, worktree: "Worktree") -> dict[str, str]:
        return {}

    def get_timeouts(self) -> dict[str, int]:
        return {}

    def get_cleanup_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        return []

    def get_health_checks(self, worktree: "Worktree") -> list["HealthCheck"]:
        return _default_health_checks(self, worktree)

    def get_readiness_probes(self, worktree: "Worktree") -> list["Probe"]:
        _ = worktree
        return []

    def get_workspace_repos(self) -> list[str]:
        if self.config.workspace_repos:
            return list(self.config.workspace_repos)
        return self.get_repos()

    def get_visual_qa_targets(self, changed_files: list[str]) -> list[str]:
        _ = changed_files
        return []

    # ── Loop hooks ───────────────────────────────────────────────────

    def is_issue_done(self, issue_data: "RawAPIDict") -> bool:
        state = issue_data.get("state")
        return isinstance(state, str) and state in {"closed", "completed"}

    def can_auto_merge(self, *, target_ref: str, thread_ref: str) -> MergeGuard:
        """Return a merge-guard verdict for an approved merge request.

        The default implementation is permissive — it always allows the merge.
        Overlays that need human-approval gates, freeze windows, or policy checks
        should override this method and return an appropriate ``MergeGuard``.

        Args:
            target_ref: The branch or ref that would be merged into.
            thread_ref: The Slack / notification thread that triggered the approval.
        """
        _ = target_ref, thread_ref
        return MergeGuard(allowed=True)
