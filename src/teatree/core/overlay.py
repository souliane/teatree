import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict, Field

from teatree.backends.types import Service
from teatree.core.gates.merge_guard import MergeGuard
from teatree.core.overlay_metadata import OverlayMetadata
from teatree.core.provision.variant import Variant
from teatree.core.worktree.health import HealthCheck
from teatree.core.worktree.health import default_health_checks as _default_health_checks
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
from teatree.utils.run import CommandFailedError, TimeoutExpired

if TYPE_CHECKING:
    from teatree.core.connector_manifest import ConnectorRequirement
    from teatree.core.factory.operational_health import HealthSignal
    from teatree.core.models import Worktree
    from teatree.core.worktree.readiness import Probe
    from teatree.types import RawAPIDict
    from teatree.utils.django_db import DjangoDbImportConfig

logger = logging.getLogger(__name__)

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
    "OverlayConnectors",
    "OverlayE2E",
    "OverlayMetadata",
    "OverlayProvisioning",
    "OverlayReview",
    "OverlayRuntime",
    "ProvisionStep",
    "RunCommand",
    "RunCommands",
    "ServiceSpec",
    "SkillMetadata",
    "SymlinkSpec",
    "ToolCommand",
    "ValidationResult",
    "Variant",
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


class OverlayConfig(BaseModel):
    """Typed, fail-closed overlay configuration (PR-27b).

    A Pydantic model: every declared field is type-validated on assignment
    (``validate_assignment=True``), so a settings module or DB overlays-registry
    override that supplies the wrong type for a known field fails LOUD instead
    of silently corrupting the config. ``extra="allow"`` keeps the overlay
    extension seam — a downstream overlay's settings module may introduce
    fields core never declared (e.g. ``dashboard_logo``, ``review_channel``),
    accessible as attributes exactly as before.

    Secrets are never stored on the model. ``*_PASS_KEY`` settings register a
    ``pass`` lookup in ``_secret_pass_keys``; the ``get_*_token`` methods read
    the store at point of use via :meth:`_read_secret`.
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True, arbitrary_types_allowed=True)

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
    # the user (#1342). Populated by ``t3 setup`` calling ``conversations.open``.
    slack_dm_channel_id: str = ""
    require_ticket: bool = False
    ready_labels: list[str] = Field(default_factory=list)
    exclude_labels: list[str] = Field(default_factory=list)
    auto_start_assigned_issues: bool = False
    max_concurrent_auto_starts: int = 1
    stale_threshold_days: int = 3
    notion_database_id: str = ""
    # The Notion page property teatree reads for a ticket's status; non-secret.
    notion_status_property: str = "Status"
    # WRITE-back gate (default OFF): ``core.sync.push_notion_status`` PATCHes the
    # Notion Status property only when this is True. Read-only otherwise.
    notion_write_back: bool = False
    mr_close_ticket: bool = False
    # When True the pre-push ship gate REJECTS any auto-close keyword instead of
    # silently rewriting it (#1012); teatree's own overlay leaves it False.
    forbid_close_keywords: bool = False
    teardown_removes_pass_entries: bool = False
    known_variants: list[str] = Field(default_factory=list)
    pr_auto_labels: list[str] = Field(default_factory=list)
    frontend_repos: list[str] = Field(default_factory=list)
    workspace_repos: list[str] = Field(default_factory=list)
    protected_branches: list[str] = Field(default_factory=list)
    # ``identity_aliases`` groups one human's handles across forges so the
    # disposition scanner can suppress self-handoff churn without conflating
    # genuinely-distinct humans (#1015).
    identity_aliases: list[list[str]] = Field(default_factory=list)
    dev_env_url: str = ""
    # Retired (#plan-gate-fsm): no handler reads it anymore; enforcement lives in
    # the Ticket state graph (STARTED → PLANNED → CODED) via ``PlanArtifact``.
    plan_gate: bool = False
    # #1295 capability J: privacy-redaction patterns scanned by the pre-publish
    # privacy gate before every public-repo write; empty in core.
    privacy_redact_terms: list[str] = Field(default_factory=list)
    privacy_block_patterns: list[str] = Field(default_factory=list)
    public_repos: list[str] = Field(default_factory=list)
    # ``owned_repos`` is the SCOPE axis (forge-host-keyed namespace patterns).
    # ORTHOGONAL to VISIBILITY (``private_repos``) and COLLABORATION
    # (``author_is_self``). Owned gates ONLY the unknown-repo approval decision,
    # never merge-without-review. See ``teatree.core.intake.repo_scope``.
    owned_repos: dict[str, list[str]] = Field(default_factory=dict)
    # Opt-in for the unknown-repo approval gate (``owned_repo_guard``). Default
    # False keeps every unmodified overlay inert; fail-CLOSED when True + owned.
    require_owned_repo_approval: bool = False
    # Per-overlay skills loaded alongside the active lifecycle skill.
    companion_skills: list[str] = Field(default_factory=list)
    # The single skill injected alongside ``/t3:review`` for a reviewer
    # sub-agent; empty string disables injection without dropping the skill.
    pr_review_companion: str = "code-review"
    # The third-party services this overlay needs wrapped as MCP tool groups.
    # Code default per overlay (settings.py tier), DB-overridable via the
    # ``overlays`` registry row; a JSON list of service names validates against
    # the ``Service`` enum and fails loud on an unknown one. Empty default =
    # an undeclared overlay wraps nothing (fail-closed).
    required_third_party_services: frozenset[Service] = Field(default_factory=frozenset)
    sentry_org: str = ""
    sentry_url: str = "https://sentry.io"

    def __init__(self, settings_module: str = "", overlay_name: str = "", **data: object) -> None:
        super().__init__(**data)
        # A plain instance dict, not a Pydantic ``PrivateAttr`` — an overlay
        # subclass may set arbitrary private (underscore) instance attributes,
        # which the ``__setattr__`` override below routes past Pydantic.
        object.__setattr__(self, "_secret_pass_keys", {})
        if settings_module:
            self._load_settings(settings_module)
        if overlay_name:
            self.apply_toml_overrides(overlay_name)

    def __setattr__(self, name: str, value: object) -> None:
        # Route past Pydantic's field machinery for the two overlay-config idioms
        # a plain object supports but a strict model does not: private state
        # (secret holders, caches — single-underscore names) and per-instance
        # method overrides (``config.get_review_channel = lambda: ...`` in tests /
        # dynamic overlays). Both land in the instance ``__dict__`` via
        # ``object.__setattr__`` so a callable override shadows the class method
        # exactly as normal Python attribute resolution does. Dunder
        # ``__pydantic_*`` internals and real data fields still go through the
        # model machinery (type validation preserved — the fail-closed contract).
        if (name.startswith("_") and not name.startswith("__")) or callable(value):
            object.__setattr__(self, name, value)
        else:
            super().__setattr__(name, value)

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
        """Apply ``[overlays.<overlay_name>]`` overrides from the DB overlays registry.

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

    def _secret_registry(self) -> dict[str, str]:
        """The ``*_PASS_KEY`` lookup dict ``__init__`` installs in the instance ``__dict__``.

        Read through ``__dict__`` (with a lazy default for a not-yet-initialised
        model) so the plain-instance-dict storage stays invisible to Pydantic's
        field machinery AND statically typed — a bare ``self._secret_pass_keys``
        access has no declared home a type checker can resolve.
        """
        return self.__dict__.setdefault("_secret_pass_keys", {})

    def _register_secret(self, attr_name: str, pass_key: str) -> None:
        self._secret_registry()[attr_name] = pass_key

    def _read_secret(self, name: str) -> str:
        """Read the ``pass`` value registered for *name* at point of use; ``""`` if unregistered."""
        pass_key = self._secret_registry().get(name)
        if not pass_key:
            return ""
        from teatree.utils.secrets import read_pass  # noqa: PLC0415

        return read_pass(pass_key)

    # ── Secret getters (override in subclass or via *_PASS_KEY) ──────

    def get_gitlab_token(self) -> str:
        return self._read_secret("gitlab_token")

    def get_gitlab_username(self) -> str:
        return self._read_secret("gitlab_username")

    def get_github_token(self) -> str:
        return self._read_secret("github_token")

    def get_slack_token(self) -> str:
        return self._read_secret("slack_token")

    def get_notion_token(self) -> str:
        # Wired via the ``notion_token_pass_key`` overlay config; default empty
        # means the runtime Notion status-sync is a clean no-op.
        return self._read_secret("notion_token")

    def get_sentry_token(self) -> str:
        return self._read_secret("sentry_token")

    # ── Structured getters (need logic, can't be plain constants) ────

    def get_review_channel(self) -> tuple[str, str]:
        return ("", "")

    def get_review_broadcast_channels(self, repo: str = "") -> list[tuple[str, str]]:
        """Return all review-broadcast channels for the overlay (#1295 capability A).

        Defaults to a single-element list wrapping :meth:`get_review_channel`
        when that getter returns a non-empty pair, else an empty list. The
        ``repo`` parameter is reserved for overlays that route by repo; the
        default implementation ignores it.
        """
        del repo  # default impl is repo-agnostic; overrides may consult it.
        channel_name, channel_id = self.get_review_channel()
        if not channel_id:
            return []
        return [(channel_name, channel_id)]

    def get_failed_e2e_watchers(self) -> list["FailedE2EWatcher"]:
        """Return failed-E2E Slack-channel watchers for the overlay (#1295 cap E); default empty."""
        return []

    def get_transition_emojis(self) -> dict[str, str]:
        override = getattr(self, "transition_emojis", None)
        if isinstance(override, dict):
            return {**DEFAULT_TRANSITION_EMOJIS, **override}
        return dict(DEFAULT_TRANSITION_EMOJIS)

    def get_review_companion_skills(self) -> list[str]:
        """Return the skills a reviewer must hold, deduped and order-preserving.

        ``[pr_review_companion, *companion_skills]``: the project's
        review-quality bar (#1135) then the overlay's standing companion skills.
        """
        return list(dict.fromkeys(s for s in [self.pr_review_companion, *self.companion_skills] if s))

    def get_lifecycle_companion_skills(self, lifecycle: str) -> list[str]:
        """Return the overlay's companion skills a *lifecycle* task must hold.

        ``review`` keeps the richer review set; every other lifecycle gets the
        standing ``companion_skills``.
        """
        if lifecycle == "review":
            return self.get_review_companion_skills()
        return [s for s in self.companion_skills if s]


# ── Overlay facets ───────────────────────────────────────────────────
#
# PR-27b: the ~44 flat ``get_*`` hooks that used to hang off ``OverlayBase``
# regroup by concern into composed facet objects (mirroring ``config`` /
# ``metadata``). Each facet's hooks stay INSTANCE methods with defaults, so an
# overlay overrides a concern by subclassing the one facet — behaviour is
# preserved exactly, and no hook is eagerly computed. ``OverlayBase`` shrinks to
# the identity/reference surface plus the facet accessors.


class OverlayProvisioning:
    """Worktree setup + environment concern — ``overlay.provisioning``."""

    def env_extra(self, worktree: "Worktree") -> dict[str, str]:
        return {}

    def declared_env_keys(self) -> set[str]:
        return set()

    _CORE_SECRET_KEYS: frozenset[str] = frozenset({"POSTGRES_PASSWORD"})

    def declared_secret_env_keys(self) -> set[str]:
        return set(self._CORE_SECRET_KEYS)

    def db_import_strategy(self, worktree: "Worktree") -> DbImportStrategy | None:
        return None

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — overlay extension-point contract; each kwarg is a documented hook input.
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

    def post_db_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        return []

    def reset_passwords_command(self, worktree: "Worktree") -> ProvisionStep | None:
        return None

    def envrc_lines(self, worktree: "Worktree") -> list[str]:
        return []

    def symlinks(self, worktree: "Worktree") -> list[SymlinkSpec]:
        return []

    def services_config(self, worktree: "Worktree") -> dict[str, ServiceSpec]:
        return {}

    def compose_file(self, worktree: "Worktree") -> str:
        return ""

    def base_images(self, worktree: "Worktree") -> list[BaseImageConfig]:
        return []

    def docker_services(self, worktree: "Worktree") -> set[str]:
        return set()

    def cleanup_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        return []

    def health_checks(self, worktree: "Worktree") -> list["HealthCheck"]:
        return _default_health_checks(self, worktree)

    def snapshot_warmer_configs(self) -> list["DjangoDbImportConfig"]:
        """Reference-DB configs the snapshot-warmer loop keeps current, one per variant.

        Default empty — an overlay with no DSLR-backed :meth:`db_import`
        strategy warms nothing. An overlay that DOES use DSLR returns one
        :class:`teatree.utils.django_db.DjangoDbImportConfig` per variant so the
        loop scanner refreshes each out-of-band (souliane/teatree#2949).
        """
        return []

    def reap_external_resources(self, worktree: "Worktree") -> list[str]:
        """Remove out-of-band resources a reaped worktree leaves behind (default none).

        Called by ``cleanup_worktree`` per torn-down worktree. The docker case: a
        compose stack leaves per-worktree containers + a multi-GB image the git/DB
        teardown never touches. Returns human-readable one-line outcomes.
        """
        return []

    def resolve_variant(self, name: str) -> Variant:
        """Resolve a variant *name* into a first-class :class:`Variant` (PR-27, #787).

        The single seam turning a bare variant name into its resolved tenant /
        language / DSLR snapshot / E2E credentials. The default returns
        :meth:`Variant.bare` — correct for an overlay that neither prefixes nor
        aliases. Consumed by ``workspace clean-all`` and overlays building a
        ``DjangoDbImportConfig``.
        """
        return Variant.bare(name)


class OverlayRuntime:
    """Run-time concern (running services, tests, probes) — ``overlay.runtime``."""

    def run_commands(self, worktree: "Worktree") -> RunCommands:
        return {}

    def pre_run_steps(self, worktree: "Worktree", service: str) -> list[ProvisionStep]:
        return []

    def test_command(self, worktree: "Worktree") -> list[str] | RunCommand:
        return []

    def lint_command(self, worktree: "Worktree") -> list[str] | RunCommand:
        """Return the argv (or ``RunCommand``) that lints this worktree.

        Backs ``t3 <overlay> run lint``. The default is empty; an overlay with a
        lint pipeline returns it here. When empty, ``run lint`` exits non-zero so
        a caller that asked to lint is not told green.
        """
        return []

    def verify_endpoints(self, worktree: "Worktree") -> dict[str, str]:
        return {}

    def readiness_probes(self, worktree: "Worktree") -> list["Probe"]:
        return []


class OverlayE2E:
    """End-to-end test concern — ``overlay.e2e``."""

    def env_extras(self, env_cache: dict[str, str]) -> dict[str, str]:
        return {}

    def run_provenance(self, spec_path: str) -> str:
        """Manifest entry id (e.g. CI lane) for *spec_path*, recorded on the run (#272); ``""`` default."""
        return ""

    def playwright_args(self, spec_path: str) -> list[str]:
        """Extra ``npx playwright test`` CLI args for *spec_path* (e.g. ``-c <config>``).

        The args-sibling of :meth:`env_extras`: a multi-config Playwright suite
        needs the runner to pass the right ``-c <config>`` per spec; the overlay
        knows the lane->config mapping, core does not. Default ``[]``.
        """
        return []

    def scenarios(self, spec_path: str) -> tuple:
        """Return the per-feature acceptance scenarios for *spec_path*; ``()`` default.

        The overlay-agnostic seam the templated-test-plan renderer reads
        scenarios through. Core never parses the scenario shape; it just threads
        the tuple to the renderer.
        """
        return ()

    def preflight(self, *, customer: str | None, base_url: str | None) -> list[Callable[[], None]]:
        return []


class OverlayReview:
    """Review / merge / customer-display concern — ``overlay.review``."""

    def merge_candidate_repo_slugs(self) -> list[str]:
        """STATIC working-repo slugs the §17.4/#2323 cross-repo merge probe binds against."""
        return []

    def can_auto_merge(self, *, target_ref: str, thread_ref: str) -> MergeGuard:
        """Return a merge-guard verdict for an approved merge request.

        The default is permissive. Overlays that need human-approval gates,
        freeze windows, or policy checks override this and return an
        appropriate ``MergeGuard``.
        """
        _ = target_ref, thread_ref
        return MergeGuard(allowed=True)

    def visual_qa_targets(self, changed_files: list[str]) -> list[str]:
        return []

    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        """True iff *changed_files* could impact what is displayed to the customer (#1967).

        The mandatory-E2E gate calls this to decide whether a change requires
        green E2E evidence before shipping / CLEAR. The default is FAIL-CLOSED —
        ``True`` for every diff and the empty diff alike — so an overlay that has
        not declared its path rules treats every change as display-impacting and
        the gate is never silently skipped.
        """
        return True


class OverlayConnectors:
    """External-connector concern (claude.ai, MCP, Slack/Notion) — ``overlay.connectors``."""

    def preflight(self) -> list[Callable[[], None]]:
        """Return zero-arg probes run before any connector-dependent loop work.

        Each callable raises ``RuntimeError`` when a connector the overlay
        hard-depends on is unreachable. Default empty — an overlay opts in only
        when it cannot function correctly with a degraded connector.
        """
        return []

    def mcp_provider_expectations(self) -> dict[str, str]:
        """``{mcp_server_name: provider}`` for the #2282 connectivity check; default empty."""
        return {}

    def manifest(self) -> list["ConnectorRequirement"]:
        """Overlay's required-vs-optional claude.ai connectors by NAME; default none (PR-19)."""
        return []


# ── Overlay base class ───────────────────────────────────────────────


class OverlayBase(ABC):
    django_app: str | None = None
    config: OverlayConfig = OverlayConfig()
    metadata: OverlayMetadata = OverlayMetadata()
    provisioning: OverlayProvisioning = OverlayProvisioning()
    runtime: OverlayRuntime = OverlayRuntime()
    e2e: OverlayE2E = OverlayE2E()
    review: OverlayReview = OverlayReview()
    connectors: OverlayConnectors = OverlayConnectors()

    # ── Required hooks ───────────────────────────────────────────────

    @abstractmethod
    def get_repos(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def get_provision_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        raise NotImplementedError

    # ── Repo identity ────────────────────────────────────────────────

    def get_workspace_repos(self) -> list[str]:
        if self.config.workspace_repos:
            return list(self.config.workspace_repos)
        return self.get_repos()

    # ── Issue / reference resolution ─────────────────────────────────

    def get_issue_title(self, url: str) -> str:
        from teatree.core.backend_registry import get_backend_provider  # noqa: PLC0415

        host = get_backend_provider().get_code_host(self)
        if host is None:
            return ""
        try:
            data = host.get_issue(url)
        except (httpx.HTTPError, CommandFailedError, TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            logger.warning("get_issue_title fetch failed for %s: %s", url, exc)
            return ""
        title = data.get("title", "") if isinstance(data, dict) else ""
        return str(title)

    def is_issue_done(self, issue_data: "RawAPIDict") -> bool:
        state = issue_data.get("state")
        return isinstance(state, str) and state in {"closed", "completed"}

    def resolve_mr_token(self, iid: int) -> str | None:
        """Return the canonical URL for ``!<iid>`` on this overlay's code host.

        The default delegates to the deterministic ``ReferenceResolver``: ``!N``
        in the ``PullRequest`` ref->URL store first, then the URL constructed from
        this overlay's ``code_host`` + the active repo's git remote slug. Returns
        ``None`` when neither resolves.
        """
        from teatree.core.reference_linkifier import ReferenceResolver  # noqa: PLC0415

        return ReferenceResolver.from_overlay(self).resolve_mr(iid)

    def resolve_issue_token(self, iid: int) -> str | None:
        """Return the canonical URL for ``#<iid>`` on this overlay's code host.

        Same DB-first, construction-fallback contract as :meth:`resolve_mr_token`.
        """
        from teatree.core.reference_linkifier import ReferenceResolver  # noqa: PLC0415

        return ReferenceResolver.from_overlay(self).resolve_issue(iid)

    # ── Loop / factory operational hooks ─────────────────────────────

    def get_timeouts(self) -> dict[str, int]:
        return {}

    def get_health_signals(self) -> list["HealthSignal"]:
        """Overlay operational-health signals for the global aggregator (PR-17; default none)."""
        return []

    def get_checking_sources(self) -> list[str]:
        """Return extra "needs you" source identifiers for ``t3 <overlay> checking show``.

        Core builds the needs-you group from overlay-agnostic rows (pending
        ``DeferredQuestion`` + failed ``TaskAttempt`` runs). An overlay that wants
        richer signals returns their identifiers here; default empty.
        """
        return []

    def get_eval_scenarios_dir(self) -> Path | None:
        """Return the directory holding overlay-contributed behavioral eval scenarios.

        Each overlay may ship its own ``*.yaml`` scenarios alongside the core
        catalog. The eval harness walks whatever directory is returned for
        ``*.yaml`` files without filesystem-scope checks — the same trust model
        as every other overlay extension hook. The path must be
        overlay-package-relative. Default ``None`` — overlays that ship no
        scenarios opt out without action.
        """
        return None
