from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
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
    "FailedE2EWatcher",
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


@dataclass(frozen=True, slots=True)
class FailedE2EWatcher:
    """One Slack-channel watcher spec for capability E (#1295).

    The loop's ``FailedE2EPostsScanner`` consumes a list of these from
    :meth:`OverlayConfig.get_failed_e2e_watchers`; each watcher tells the
    scanner which channel to poll, how to recognise a failed-E2E post in
    that channel, how to extract the failing spec path from one bullet,
    and which agent skill to dispatch with the extracted spec.

    ``post_pattern`` is a regex applied to the *message text* — a match
    means "this is a failed-E2E post". ``spec_pattern`` is a regex
    applied to one bullet line and must yield the spec path in either
    group(1) or the named group ``spec``; non-matching bullets are
    skipped. ``agent_skill`` is the skill name (e.g. ``"t3:e2e"``) the
    dispatcher routes the resulting signal to.
    """

    channel_id: str
    post_pattern: str
    spec_pattern: str
    agent_skill: str = "t3:e2e"


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
    # Setup-time provisioned IM channel id between the per-overlay bot and
    # the user (#1342). Populated by ``t3 setup`` calling
    # ``conversations.open`` once and persisting the result back to
    # ``[overlays.<name>] slack_dm_channel_id`` in ``~/.teatree.toml``.
    # ``SlackBotBackend.open_dm`` short-circuits to this value for the
    # configured ``slack_user_id`` so DMs route through this bot's IM
    # rather than re-deriving the channel (which fails ``channel_not_found``
    # for a freshly-registered per-overlay bot and silently falls back
    # through whichever bot already has an IM with the user).
    slack_dm_channel_id: str = ""
    require_ticket: bool = False
    ready_labels: list[str]
    exclude_labels: list[str]
    auto_start_assigned_issues: bool = False
    max_concurrent_auto_starts: int = 1
    stale_threshold_days: int = 3
    notion_database_id: str = ""
    mr_close_ticket: bool = False
    # When True the pre-push ship gate REJECTS any auto-close keyword
    # (Closes/Fixes/Resolves #N, full-URL forms) in the MR description or
    # any commit body on the branch, instead of silently rewriting it.
    # An overlay sets this when issue closure is managed via the forge's
    # linked-items API rather than auto-close trailers (#1012); teatree's
    # own overlay leaves it False (teatree PRs legitimately use ``Closes #N``).
    forbid_close_keywords: bool = False
    teardown_removes_pass_entries: bool = False
    known_variants: list[str]
    pr_auto_labels: list[str]
    frontend_repos: list[str]
    workspace_repos: list[str]
    protected_branches: list[str]
    # ``identity_aliases`` groups one human's handles across forges so the
    # disposition scanner can suppress self-handoff churn without conflating
    # genuinely-distinct humans (#1015). Shape: each inner list is one human's
    # aliases (e.g. ``[["a-github", "a-gitlab", "a.work"], ["b-github"]]``);
    # cross-group reassigns stay visible because they cross human boundaries.
    identity_aliases: list[list[str]]
    dev_env_url: str = ""
    # When True the PreToolUse plan-gate denies ``Edit``/``Write`` on any file
    # under ``$T3_WORKSPACE_DIR`` unless either ``/plan`` has been invoked or
    # the touched file was ``Read`` earlier in the session (see
    # ``hooks/scripts/hook_router.py::handle_enforce_plan_gate``). The gate is
    # opt-in per overlay — default ``False`` means agents working on this
    # overlay's repos are not subject to the gate until the user flips it in
    # ``[overlays.<name>] plan_gate = true``. The gate consults ALL overlays'
    # ``plan_gate`` values: if any overlay opts in, the gate is active for
    # workspace-scoped edits on this machine. Outside ``$T3_WORKSPACE_DIR``
    # (e.g. ``~/.zshrc``, ``~/.claude/``, agent memory) the gate is silent.
    plan_gate: bool = False
    # #1295 capability J: privacy-redaction patterns scanned by the
    # pre-publish privacy gate before every public-repo write. Lists are
    # empty in core; each overlay supplies its own customer-domain
    # acronyms, internal org prefixes, and quote-anchor patterns. The
    # gate fires only when the target repo is in ``public_repos``.
    privacy_redact_terms: list[str]
    privacy_block_patterns: list[str]
    public_repos: list[str]
    # ``companion_skills`` is a per-overlay list of skill names that must be
    # loaded alongside the active lifecycle skill — the standing equivalent of
    # "always load /ac-django and /ac-python when working in this overlay".
    # Wired through ``SkillLoadingPolicy._base_detected_skills`` so the
    # existing ``resolve_companions`` resolver handles the dependency chain
    # without a parallel implementation.
    companion_skills: list[str]
    # ``pr_review_companion`` is the single skill injected alongside
    # ``/t3:review`` whenever a reviewer sub-agent is dispatched
    # (``phase == "reviewing"``). The global default ``"code-review"`` carries
    # the project's review-quality bar; an overlay overrides via
    # ``[overlays.<name>] pr_review_companion = "receiving-code-review"`` (or
    # any other skill) in ``~/.teatree.toml``. An empty string disables
    # injection without removing the lifecycle skill (#1135).
    pr_review_companion: str = "code-review"

    def __init__(self, settings_module: str = "", overlay_name: str = "") -> None:
        # Initialize mutable defaults per-instance
        self.known_variants = []
        self.pr_auto_labels = []
        self.frontend_repos = []
        self.workspace_repos = []
        self.protected_branches = []
        self.ready_labels = []
        self.exclude_labels = []
        self.identity_aliases = []
        self.companion_skills = []
        self.privacy_redact_terms = []
        self.privacy_block_patterns = []
        self.public_repos = []
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

    def get_review_broadcast_channels(self, repo: str = "") -> list[tuple[str, str]]:
        """Return all review-broadcast channels for the overlay (#1295 capability A).

        Defaults to a single-element list wrapping :meth:`get_review_channel`
        when that getter returns a non-empty pair, else an empty list. This
        keeps every legacy caller (review request guard, slack review sync,
        slack broadcast scanner) backward-compatible: an overlay that only
        sets ``review_channel`` continues to broadcast to one channel; an
        overlay that needs a multi-channel fan-out (per-repo, per-team)
        overrides this method without touching the legacy single-channel
        accessor.

        The ``repo`` parameter is reserved for overlays that route by repo
        (e.g. one channel per repo group); the default implementation
        ignores it.
        """
        del repo  # default impl is repo-agnostic; overrides may consult it.
        channel_name, channel_id = self.get_review_channel()
        if not channel_id:
            return []
        return [(channel_name, channel_id)]

    def get_failed_e2e_watchers(self) -> list["FailedE2EWatcher"]:
        """Return failed-E2E Slack-channel watchers for the overlay (#1295 cap E).

        Each watcher tells the loop which Slack channel publishes failed-E2E
        notifications, the regex that recognises one (``post_pattern``), the
        regex that extracts the failing spec path (``spec_pattern``), and
        the agent skill to dispatch (``agent_skill``). Default is empty:
        teatree-core does not watch any channel out of the box; downstream
        overlays supply watchers.
        """
        return []

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

    def get_dslr_tenant_for_variant(self, variant: str) -> str:
        """Return the DSLR snapshot tenant name for *variant*.

        Overlays whose DB-import strategy uses DSLR translate the
        ``Ticket.variant`` string into the tenant suffix that appears in
        DSLR snapshot names. Two transformations may stack:

        1.  **Prefix** — e.g. an overlay may turn ``client-a`` into the
            tenant ``development-client-a`` so the snapshot key carries
            the environment alongside the tenant identity.
        2.  **Alias** — a child variant whose data is identical to its
            parent (e.g. ``client-a-regional`` shares snapshots with
            ``client-a``) maps to the parent's tenant so the snapshot
            lookup actually finds the right file (#1306).

        The default returns the variant verbatim, which is correct for
        overlays that don't prefix or alias the tenant. Used by
        ``workspace clean-all`` to compute the in-use tenant set, and by
        overlays computing ``DjangoDbImportConfig.ref_db_name`` for the
        DSLR snapshot lookup.
        """
        return variant

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

    def get_connector_preflight(self) -> list[Callable[[], None]]:
        """Return zero-arg probes run before any connector-dependent loop work.

        Each callable raises ``RuntimeError`` (caught by the loop
        entrypoint, which then ``raise SystemExit``) when a connector the
        overlay hard-depends on (Slack, Notion, claude.ai) is unreachable.
        The default is empty — an overlay opts in only when it cannot
        function correctly with a degraded connector (silent no-ops are
        worse than refusing to start). Analogous to
        :meth:`get_e2e_preflight` but fired at loop/lifecycle start
        rather than before an E2E run.
        """
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

    def get_eval_scenarios_dir(self) -> Path | None:
        """Return the directory holding overlay-contributed behavioral eval scenarios.

        Each overlay may ship its own ``*.yaml`` scenarios alongside the
        core catalog under ``src/teatree/eval/scenarios/``. The eval
        harness walks every overlay's directory at discovery time
        (`teatree.eval.discovery`), so scenarios that reference
        tenant-specific identities live in the relevant overlay and the
        core directory keeps only the cross-overlay invariants.

        The returned path must be overlay-package-relative (e.g.
        ``Path(__file__).parent / "eval" / "scenarios"``). The harness
        trusts the overlay author — it walks whatever directory is
        returned for ``*.yaml`` files without filesystem-scope checks —
        so a misconfigured overlay pointing at ``/`` or ``~/.ssh`` would
        cause the harness to try to load every YAML it finds there. This
        matches the trust model of every other ``get_*`` extension hook
        on ``OverlayBase``.

        Default returns ``None`` — overlays that do not ship scenarios
        opt out without action.
        """
        return None

    # ── Loop hooks ───────────────────────────────────────────────────

    def is_issue_done(self, issue_data: "RawAPIDict") -> bool:
        state = issue_data.get("state")
        return isinstance(state, str) and state in {"closed", "completed"}

    def resolve_mr_token(self, iid: int) -> str | None:
        """Return the canonical URL for ``!<iid>`` on this overlay's code host.

        Overridden by overlays that own merge/pull requests across multiple
        repositories and can resolve a bare ``!N`` to its repo's web URL.
        The default returns ``None`` —
        :func:`teatree.slack_mrkdwn.slack_linkify` leaves the bare token
        unrewritten when no resolver can claim it, so the Slack reader sees
        inert text rather than a guessed-wrong URL.
        """
        _ = iid
        return None

    def resolve_issue_token(self, iid: int) -> str | None:
        """Return the canonical URL for ``#<iid>`` on this overlay's code host.

        Default ``None``, for the same reason as :meth:`resolve_mr_token`.
        """
        _ = iid
        return None

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
