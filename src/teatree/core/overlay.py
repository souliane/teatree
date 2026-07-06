import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from teatree.core.gates.merge_guard import MergeGuard
from teatree.core.overlay_metadata import OverlayMetadata
from teatree.core.variant import Variant
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
    from teatree.core.models import Worktree
    from teatree.core.operational_health import HealthSignal
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
    "OverlayMetadata",
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
    # Retired (#plan-gate-fsm): the wall-clock ``handle_enforce_plan_gate``
    # PreToolUse gate (opt-in per overlay) was replaced by the ``PLANNED`` FSM
    # state. The field is kept for migration compatibility but no handler reads
    # it anymore; the enforcement now lives in the Ticket state graph
    # (STARTED → PLANNED → CODED) via ``PlanArtifact``.
    plan_gate: bool = False
    # #1295 capability J: privacy-redaction patterns scanned by the
    # pre-publish privacy gate before every public-repo write. Lists are
    # empty in core; each overlay supplies its own customer-domain
    # acronyms, internal org prefixes, and quote-anchor patterns. The
    # gate fires only when the target repo is in ``public_repos``.
    privacy_redact_terms: list[str]
    privacy_block_patterns: list[str]
    public_repos: list[str]
    # ``owned_repos`` is the SCOPE axis: the (forge-host, namespace) repos this
    # overlay legitimately works on. ORTHOGONAL to VISIBILITY (``private_repos``,
    # public-vs-private leak-prevention read by the publish hooks) and to
    # COLLABORATION (solo-vs-shared, the author/review gate in
    # ``teatree.core.review_candidate``). Owned means the agent may work and push
    # freely; it does NOT imply auto-merge — a shared repo is still in scope yet
    # still needs colleague review, so ``owned_repos`` gates ONLY the
    # unknown-repo approval decision, never merge-without-review.
    #
    # Shape: ``{normalized-host: [host-relative-namespace-pattern, ...]}``. The
    # key is the canonical host (``"github.com"``, ``"gitlab.com"``, self-hosted
    # ``"gitlab.acme.internal"``); making it forge-host-keyed means a host-blind
    # entry is structurally impossible — ``gitlab.com/souliane/x`` can never
    # reach a ``github.com`` pattern list. Each value pattern is matched by
    # ``slug_namespace_matches`` (segment-bounded) AFTER host equality, so
    # ``"souliane"`` covers every ``souliane/<repo>`` and ``"acme-eng/widget-overlay"``
    # covers that one repo. A sole-element ``["*"]`` is the whole-host wildcard,
    # reserved for dedicated self-hosted forges — NEVER on github.com/gitlab.com.
    # A ``[overlays.<name>.owned_repos]`` TOML table REPLACES the settings dict
    # (authoritative-and-complete, no deep-merge). When empty (default) the
    # overlay has not opted into scope gating. Consumed by
    # ``teatree.core.repo_scope`` / ``teatree.core.gates.owned_repo_guard``.
    owned_repos: dict[str, list[str]]
    # Opt-in for the unknown-repo approval gate (``owned_repo_guard``). Default
    # False keeps every unmodified overlay inert. When True AND ``owned_repos``
    # is non-empty, a push/merge to a repo no host/namespace pattern owns is held
    # for the operator (fail-CLOSED on a clean "unknown" verdict — the OPPOSITE
    # polarity to the visibility gate, which fails open). An empty ``owned_repos``
    # under this flag still passes (misconfig guard — never block-everything).
    require_owned_repo_approval: bool = False
    # ``companion_skills`` is a per-overlay list of skill names that must be
    # loaded alongside the active lifecycle skill — the standing equivalent of
    # "always load /ac-django and /ac-python when working in this overlay".
    # Wired through ``SkillLoadingPolicy._base_detected_skills`` so the
    # ``resolve_requires`` chain handles the dependency closure without a
    # parallel implementation.
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
        # List-typed config fields reset to a fresh empty list per instance
        # (mutable-default avoidance); ``owned_repos`` (a dict) is separate.
        list_fields = (
            "known_variants pr_auto_labels frontend_repos workspace_repos protected_branches ready_labels "
            "exclude_labels identity_aliases companion_skills privacy_redact_terms privacy_block_patterns public_repos"
        )
        for field in list_fields.split():
            setattr(self, field, [])
        self.owned_repos = {}
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

    def get_review_companion_skills(self) -> list[str]:
        """Return the skills a reviewer must hold, deduped and order-preserving.

        ``[pr_review_companion, *companion_skills]``: the project's
        review-quality bar (#1135) then the overlay's standing companion skills,
        threaded through :func:`active_overlay_review_skills` into the
        reviewing-phase bundle and system context so a headless reviewer
        receives the overlay's review conventions in full. An overlay broadens
        the set by overriding this method; core stays overlay-agnostic.
        """
        return list(dict.fromkeys(s for s in [self.pr_review_companion, *self.companion_skills] if s))

    def get_lifecycle_companion_skills(self, lifecycle: str) -> list[str]:
        """Return the overlay's companion skills a *lifecycle* task must hold.

        Generalizes :meth:`get_review_companion_skills` beyond reviewing so a
        fanned-out ``code``/``e2e``/``test`` task demands the overlay's
        companion skills too. ``review`` keeps the richer review set; every other
        lifecycle gets the standing ``companion_skills``.
        """
        if lifecycle == "review":
            return self.get_review_companion_skills()
        return [s for s in self.companion_skills if s]


# ── Overlay base class ───────────────────────────────────────────────


# ast-grep-ignore: ac-django-no-complexity-suppressions
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

    def resolve_variant(self, name: str) -> Variant:
        """Resolve a variant *name* into a first-class :class:`Variant` (PR-27, #787).

        The single seam turning a bare variant name into its resolved tenant /
        language / DSLR snapshot / E2E credentials — no overlay hook takes a raw
        ``variant: str`` any more. An override may stack a **prefix**
        (``client-a`` → tenant ``development-client-a``) and an **alias** (a
        child variant maps to the parent's ``canonical_tenant`` so a shared
        snapshot resolves, #1306). The default returns :meth:`Variant.bare` — the
        tenant is the name verbatim — correct for an overlay that neither
        prefixes nor aliases. Consumed by ``workspace clean-all`` (in-use tenant
        set) and by overlays building a ``DjangoDbImportConfig``.
        """
        return Variant.bare(name)

    def get_snapshot_warmer_configs(self) -> list["DjangoDbImportConfig"]:
        """Reference-DB configs the snapshot-warmer loop keeps current, one per variant.

        Default empty — an overlay with no DSLR-backed :meth:`db_import`
        strategy (like teatree's own dogfood overlay) warms nothing. An
        overlay that DOES use DSLR returns one
        :class:`teatree.utils.django_db.DjangoDbImportConfig` per configured
        variant/tenant so the loop scanner
        (:mod:`teatree.loop.scanners.snapshot_warmer`) can refresh each
        out-of-band — a ticket-critical-path provision then never has to pay
        the slow restore+migrate path itself (souliane/teatree#2949).
        """
        return []

    # ast-grep-ignore: ac-django-no-complexity-suppressions
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

    # ── Run hooks ────────────────────────────────────────────────────

    def get_run_commands(self, worktree: "Worktree") -> RunCommands:
        return {}

    def get_pre_run_steps(self, worktree: "Worktree", service: str) -> list[ProvisionStep]:
        return []

    def get_test_command(self, worktree: "Worktree") -> list[str] | RunCommand:
        return []

    def get_lint_command(self, worktree: "Worktree") -> list[str] | RunCommand:
        """Return the argv (or ``RunCommand``) that lints this worktree.

        Backs ``t3 <overlay> run lint`` — the single entry point for "lint
        this worktree", mirroring :meth:`get_test_command` for ``run tests``.
        The default is empty; an overlay that has a lint pipeline (usually its
        ``prek``/``pre-commit`` config) returns it here. When empty, ``run
        lint`` exits non-zero so a caller that asked to lint is not told green.
        """
        return []

    def get_e2e_env_extras(self, env_cache: dict[str, str]) -> dict[str, str]:
        _ = env_cache
        return {}

    def get_e2e_run_provenance(self, spec_path: str) -> str:
        """Manifest entry id (e.g. CI lane) for *spec_path*, recorded on the run (#272); ``""`` default."""
        return ""

    def get_e2e_playwright_args(self, spec_path: str) -> list[str]:
        """Extra ``npx playwright test`` CLI args for *spec_path* (e.g. ``-c <config>``).

        The args-sibling of :meth:`get_e2e_env_extras`: where that hook
        contributes environment variables, this contributes Playwright CLI
        flags the overlay derives from the spec. A multi-config Playwright
        suite (one config per lane — api-flow vs contrib vs portal) needs the
        runner to pass the right ``-c <config>`` per spec; the overlay knows
        the lane->config mapping, core does not. Default ``[]`` — no flags, so
        every overlay that does not override keeps the exact prior behaviour.
        """
        del spec_path
        return []

    def get_e2e_scenarios(self, spec_path: str) -> tuple:
        """Return the per-feature acceptance scenarios for *spec_path*; ``()`` default.

        The overlay-agnostic seam the templated-test-plan renderer reads scenarios
        through — analogous to :meth:`get_e2e_run_provenance`, which resolves a spec
        to its CI lane. Core never parses the scenario shape (each element is an
        overlay-defined frozen ``Scenario`` carrying
        ``surface``/``title``/``preconditions``/``steps``/``expected``/``modality``/``captures``);
        it just threads the tuple to the renderer. The default empty tuple keeps an
        overlay with no scenario manifest inert, so every registered overlay resolves
        without an override.
        """
        del spec_path
        return ()

    def get_e2e_preflight(self, *, customer: str | None, base_url: str | None) -> list[Callable[[], None]]:
        _ = customer, base_url
        return []

    def get_connector_preflight(self) -> list[Callable[[], None]]:
        """Return zero-arg probes run before any connector-dependent loop work.

        Each callable raises ``RuntimeError`` (caught by the loop entrypoint, which then ``raise
        SystemExit``) when a connector the overlay hard-depends on (Slack, Notion, claude.ai) is
        unreachable. The default is empty — an overlay opts in only when it cannot function correctly
        with a degraded connector (silent no-ops are worse than refusing to start). Analogous to
        :meth:`get_e2e_preflight` but fired at loop/lifecycle start rather than before an E2E run.
        """
        return []

    def get_mcp_provider_expectations(self) -> dict[str, str]:
        """``{mcp_server_name: provider}`` for the #2282 connectivity check (default empty; real values in #251)."""
        return {}

    def get_connector_manifest(self) -> list["ConnectorRequirement"]:
        """Overlay's required-vs-optional claude.ai connectors by NAME; default none (PR-19)."""
        return []

    def get_verify_endpoints(self, worktree: "Worktree") -> dict[str, str]:
        return {}

    def get_timeouts(self) -> dict[str, int]:
        return {}

    def get_cleanup_steps(self, worktree: "Worktree") -> list[ProvisionStep]:
        return []

    def reap_worktree_external_resources(self, worktree: "Worktree") -> list[str]:
        """Remove out-of-band resources a reaped worktree leaves behind (default none).

        Called by ``cleanup_worktree`` per torn-down worktree. The docker case: a
        compose stack leaves per-worktree containers + a multi-GB image the git/DB
        teardown never touches, reaped through :mod:`teatree.docker.reap` (core
        stays overlay-agnostic). Returns human-readable one-line outcomes; the
        default ``[]`` opts an overlay with no external resources out.
        """
        _ = worktree
        return []

    def get_health_signals(self) -> list["HealthSignal"]:
        """Overlay operational-health signals for the global aggregator (PR-17; default none)."""
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

    def get_merge_candidate_repo_slugs(self) -> list[str]:
        """STATIC working-repo slugs the §17.4/#2323 cross-repo merge probe binds against (``pr_slug_resolution``)."""
        return []

    def get_visual_qa_targets(self, changed_files: list[str]) -> list[str]:
        _ = changed_files
        return []

    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        """True iff *changed_files* could impact what is displayed to the customer (#1967).

        The mandatory-E2E gate (:mod:`teatree.core.gates.e2e_mandatory_gate`) calls
        this to decide whether a change requires green E2E evidence before
        shipping / CLEAR. The default is FAIL-CLOSED — it returns ``True`` for
        every non-empty diff and for the empty diff alike — so an overlay that
        has not declared its path rules treats every change as
        display-impacting and the gate is never silently skipped.

        An overlay declares its non-impacting paths (tests, migrations,
        tooling) and returns
        :func:`~teatree.core.customer_display_impact.classify_paths`; an overlay
        with no customer surfaces at all (the dogfood overlay) returns
        ``False``.
        """
        _ = changed_files
        return True

    def get_eval_scenarios_dir(self) -> Path | None:
        """Return the directory holding overlay-contributed behavioral eval scenarios.

        Each overlay may ship its own ``*.yaml`` scenarios alongside the
        core catalog under ``evals/scenarios/``. The eval
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

    def get_checking_sources(self) -> list[str]:
        """Return extra "needs you" source identifiers for ``t3 <overlay> checking show``.

        The ``/t3:checking`` report's needs-you group is built in core from
        overlay-agnostic rows (pending ``DeferredQuestion`` + failed
        ``TaskAttempt`` runs — the durable "blocked" proxy). Core never makes
        a live forge call. An overlay that wants richer needs-you signals
        (e.g. ``RedCardSignal`` or ``ScannedFailedE2E``) opts in by returning
        their identifiers here; the default is empty, so an overlay that does
        not override it contributes nothing beyond the core sources.
        """
        return []

    def is_issue_done(self, issue_data: "RawAPIDict") -> bool:
        state = issue_data.get("state")
        return isinstance(state, str) and state in {"closed", "completed"}

    def resolve_mr_token(self, iid: int) -> str | None:
        """Return the canonical URL for ``!<iid>`` on this overlay's code host.

        The default delegates to the deterministic
        :class:`~teatree.core.reference_linkifier.ReferenceResolver`: it looks
        ``!N`` up in the ``PullRequest`` ref->URL store first, then constructs
        the URL from this overlay's ``code_host`` + the active repo's git
        remote slug. Returns ``None`` when neither resolves —
        :func:`teatree.slack_mrkdwn.slack_linkify` and
        :func:`teatree.core.reference_linkifier.linkify` then leave the bare
        token untouched (the gate's fallback) rather than guess a wrong URL.
        Overlays may still override for bespoke multi-repo resolution.
        """
        from teatree.core.reference_linkifier import ReferenceResolver  # noqa: PLC0415

        return ReferenceResolver.from_overlay(self).resolve_mr(iid)

    def resolve_issue_token(self, iid: int) -> str | None:
        """Return the canonical URL for ``#<iid>`` on this overlay's code host.

        Same DB-first, construction-fallback contract as
        :meth:`resolve_mr_token`, resolving the ``#N`` issue (or, via the
        ``PullRequest`` store, a PR number) instead.
        """
        from teatree.core.reference_linkifier import ReferenceResolver  # noqa: PLC0415

        return ReferenceResolver.from_overlay(self).resolve_issue(iid)

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
