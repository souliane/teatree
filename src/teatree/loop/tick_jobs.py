"""Scanner-job construction for the loop tick.

Build the per-overlay fan-out of scanner jobs that ``run_tick``
executes in parallel. Split out of ``tick.py`` to keep the
orchestrator under the module-health LOC gate; the orchestrator
delegates to ``build_default_jobs`` and ``build_default_scanners``.
"""

import datetime as _dt
import logging
import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.config import (
    Autonomy,
    Mode,
    discover_active_overlay,
    discover_overlays,
    get_effective_settings,
    load_config,
    workspace_dir,
)
from teatree.core.clone_paths import find_clone_path

if TYPE_CHECKING:
    from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.core.models import ImplementedIssueMarker
from teatree.loop.scanners import (
    ActiveTicketsScanner,
    ArchitecturalReviewScanner,
    AssignedIssuesScanner,
    BackendChannelHistoryFetcher,
    CallCommandMergeKeystone,
    CodexReviewScanner,
    EvalLocalScanner,
    GhCodexPrApi,
    GhPrApiClient,
    GitLabApprovalsScanner,
    GlabGhMrStateClassifier,
    IncomingEventsScanner,
    IssueImplementerScanner,
    MyPrsScanner,
    NotionViewScanner,
    NullMergeNotifier,
    OutboundAuditScanner,
    PendingTasksScanner,
    PrSweepScanner,
    PullMainCloneScanner,
    RedCardScanner,
    ResourcePressureScanner,
    ReviewerPrsScanner,
    ReviewNagScanner,
    ReviewRequestMergeReactScanner,
    Scanner,
    ScanningNewsScanner,
    SelfUpdateScanner,
    SlackBroadcastsScanner,
    SlackDmInboundScanner,
    SlackMentionsScanner,
    SlackMergeNotifier,
    SlackReviewIntentScanner,
    StaleTicketsScanner,
    TicketCompletionScanner,
    TicketDispositionScanner,
    TodoSweepScanner,
)
from teatree.loop.scanners.base import ScannerError, ScanSignal
from teatree.loop.scanners.notion_view import NotionLike
from teatree.loop.scanners.self_update_ci import GhMainCiStatus
from teatree.loop.tick_resolvers import (
    _allowed_url_prefixes_for_host,
    _identity_alias_groups_for_overlay,
    _web_origin_for_host,
)
from teatree.messaging import notify_with_fallback
from teatree.notify import NotifyKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ScannerJob:
    """Internal record pairing a scanner with its overlay tag."""

    scanner: Scanner
    overlay: str


class Domain(StrEnum):
    """A partition of the per-tick scanner fan-out (#1482).

    The per-overlay members own disjoint, exhaustive slices of
    :func:`_jobs_for_overlay_backend`; :data:`PER_OVERLAY_DOMAINS`
    summed reproduces it exactly. ``DISPATCH`` is the global
    (non-overlay) triad ``build_default_jobs`` hard-codes — it is the
    one member excluded from the per-overlay sum.
    """

    TICKETS = "tickets"
    SHIP = "ship"
    REVIEW = "review"
    FOLLOWUP = "followup"
    INBOX = "inbox"
    ARCH_REVIEW = "arch_review"
    AUDIT = "audit"
    HOUSEKEEPING = "housekeeping"
    ISSUE_IMPLEMENTER = "issue_implementer"
    DISPATCH = "dispatch"


#: The :class:`Domain` members whose slices partition the per-overlay
#: fan-out. ``_jobs_for_overlay_backend(backend) ==
#: sum(jobs_for_domain(d, backend) for d in PER_OVERLAY_DOMAINS)`` by
#: construction; the parity guard in ``tests/teatree_loop`` pins it.
PER_OVERLAY_DOMAINS: tuple[Domain, ...] = (
    Domain.TICKETS,
    Domain.SHIP,
    Domain.REVIEW,
    Domain.FOLLOWUP,
    Domain.INBOX,
    Domain.ARCH_REVIEW,
    Domain.AUDIT,
    Domain.HOUSEKEEPING,
    Domain.ISSUE_IMPLEMENTER,
)


def _jobs_for_backend_hosts(
    backend: OverlayBackends,
    tag: str,
    *,
    all_backends: tuple[OverlayBackends, ...] = (),
) -> list[_ScannerJob]:
    """Build one scanner-job fan-out per host on *backend* (#976).

    Pre-fix the caller assumed one ``backend.host``; with multi-host the
    same fan-out must run for each platform that resolved a credential.
    ``TicketCompletionScanner`` is overlay-scoped (reads local Ticket
    rows), so it's emitted exactly once even when two hosts are present.

    *all_backends* (when provided) lets each scanner know the URL claims
    of sibling overlays so a less-specific claim here yields to a more
    specific claim there — see :func:`_competing_url_prefixes` (#1324).
    """
    jobs: list[_ScannerJob] = []
    ticket_completion_emitted = False
    gitlab_approvals_enabled = _gitlab_approvals_enabled()
    identity_groups = _identity_alias_groups_for_overlay(tag, backend)
    # #1113 Defect 1: the trusted operator identity set (``backend.identities``,
    # #976) is an implicit self-group when no explicit ``identity_aliases``
    # config overrides it. Without this union, ``user_identity_aliases`` and
    # ``identity_alias_groups`` both resolve to empty in the user's deployment
    # → ``_is_self_handoff`` short-circuits to False → same-human reassigns
    # between ``backend.identities`` members (the multi-identity operator set)
    # render as ``reassigned`` churn. Explicit groups still take precedence.
    if not identity_groups and len(backend.identities) > 1:
        identity_groups = (tuple(backend.identities),)
    for code_host in backend.hosts:
        url_prefixes = _allowed_url_prefixes_for_host(backend, code_host)
        competing_prefixes = _competing_url_prefixes(
            this_backend=backend,
            code_host=code_host,
            all_backends=all_backends,
        )
        jobs.extend(
            [
                _ScannerJob(
                    scanner=MyPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        allowed_url_prefixes=url_prefixes,
                        competing_url_prefixes=competing_prefixes,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=ReviewerPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        overlay_name=tag,
                        allowed_url_prefixes=url_prefixes,
                        competing_url_prefixes=competing_prefixes,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=AssignedIssuesScanner(
                        host=code_host,
                        ready_labels=backend.ready_labels,
                        exclude_labels=backend.exclude_labels,
                        auto_start=backend.auto_start_assigned_issues,
                        max_concurrent=backend.max_concurrent_auto_starts,
                        overlay_name=tag,
                        identities=backend.identities,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=TicketDispositionScanner(
                        host=code_host,
                        overlay=backend.overlay,
                        ready_labels=backend.ready_labels,
                        overlay_name=tag,
                        user_identity_aliases=_user_identity_aliases_for_overlay(tag),
                        identity_alias_groups=identity_groups,
                    ),
                    overlay=tag,
                ),
            ],
        )
        if backend.overlay is not None and not ticket_completion_emitted:
            jobs.append(
                _ScannerJob(
                    scanner=TicketCompletionScanner(
                        overlay=backend.overlay,
                        overlay_name=tag,
                    ),
                    overlay=tag,
                ),
            )
            ticket_completion_emitted = True
        if gitlab_approvals_enabled:
            # Poll-driven complement to the webhook-driven `SCHEDULE_MERGE` path
            # (#936). Off by default — opt-in via the env flag so deployments
            # that already wire the GitLab webhook do not double-emit.
            jobs.append(
                _ScannerJob(
                    scanner=GitLabApprovalsScanner(host=code_host, identities=backend.identities),
                    overlay=tag,
                ),
            )
    return jobs


_TUPLE_PAIR = 2


def _competing_url_prefixes(
    *,
    this_backend: OverlayBackends,
    code_host: CodeHostBackend,
    all_backends: tuple[OverlayBackends, ...],
) -> tuple[str, ...]:
    """Collect URL claims from every overlay OTHER than *this_backend* (#1324).

    Lets a scanner reject a URL it claims less specifically than a sibling
    overlay claims — the most-specific overlay attribution wins, so a
    dogfooding overlay that lists a sibling's repo path under
    ``workspace_repos`` no longer steals the sibling's PRs from its zone.

    Only sibling backends with a code-host that resolves to the same web
    origin contribute claims; a GitLab-only sibling can't compete for a
    GitHub URL.
    """
    if not all_backends:
        return ()
    own_origin = _web_origin_for_host(code_host)
    if not own_origin:
        return ()
    prefixes: list[str] = []
    for sibling in all_backends:
        if sibling is this_backend or sibling.name == this_backend.name:
            continue
        for sibling_host in sibling.hosts:
            if _web_origin_for_host(sibling_host) != own_origin:
                continue
            prefixes.extend(_allowed_url_prefixes_for_host(sibling, sibling_host))
    return tuple(prefixes)


def _resolve_broadcast_channels(config: object) -> list[tuple[str, str]]:
    """Read overlay broadcast-channel list with legacy fallback (#1295 cap A)."""
    pairs: list[tuple[str, str]] = []
    multi_getter = getattr(config, "get_review_broadcast_channels", None)
    if callable(multi_getter):
        try:
            raw = multi_getter()
        except TypeError:
            raw = None
        if isinstance(raw, list):
            pairs = [pair for pair in raw if isinstance(pair, tuple) and len(pair) == _TUPLE_PAIR]
    if not pairs:
        legacy_getter = getattr(config, "get_review_channel", None)
        if callable(legacy_getter):
            legacy = legacy_getter()
            if isinstance(legacy, tuple) and len(legacy) == _TUPLE_PAIR and legacy[1]:
                pairs = [legacy]
    return pairs


def _own_author_identity(backend: OverlayBackends) -> str:
    """Resolve the user's forge username for the own-MR review skip (#1844 L3).

    The own-author ``:eyes:``-and-dispatch skip in
    :class:`SlackBroadcastsScanner` needs to know who "we" are. Deriving
    this from ``overlay.config.get_gitlab_username()`` breaks for every
    overlay that leaves the getter at the core default ``""`` — an empty
    value disables the skip and the loop reviews the user's own MRs. The
    self-identity source of truth is the same one
    :class:`ReviewerPrsScanner` uses: ``backend.identities`` (the
    multi-alias operator set) with a ``host.current_user()`` fallback, so
    the skip works regardless of whether an overlay implements the getter.
    """
    if backend.identities:
        return backend.identities[0]
    for host in backend.hosts:
        user = host.current_user()
        if user:
            return user
    return ""


def _slack_broadcasts_scanner_for(backend: OverlayBackends) -> SlackBroadcastsScanner | None:
    """Build a per-overlay broadcast scanner from the overlay's review channel (#1255).

    The scanner polls the overlay's configured review channel for
    MR-link broadcasts so a reviewer-role tag in a Slack-Connect review team triggers the same downstream dispatch as a direct ``:eyes:``
    reaction. Returns ``None`` when the overlay has no Python class
    (TOML-only), no messaging backend resolved, or no review channel
    configured — those three combinations make the scanner a no-op.
    """
    overlay = backend.overlay
    if overlay is None or backend.messaging is None:
        return None
    channels_pairs = _resolve_broadcast_channels(overlay.config)
    channel_ids = [cid for _name, cid in channels_pairs if cid]
    if not channel_ids:
        return None
    glab_token = overlay.config.get_gitlab_token() if hasattr(overlay.config, "get_gitlab_token") else ""
    github_token = overlay.config.get_github_token() if hasattr(overlay.config, "get_github_token") else ""
    current_gitlab_username = _own_author_identity(backend)
    return SlackBroadcastsScanner(
        backend=backend.messaging,
        channels=channel_ids,
        fetch_channel_history=BackendChannelHistoryFetcher(backend=backend.messaging),
        classify_mrs=GlabGhMrStateClassifier(glab_token=glab_token, github_token=github_token),
        overlay=backend.name,
        current_gitlab_username=current_gitlab_username,
    )


def _pr_sweep_scanner_for(backend: OverlayBackends, *, slack_user_id: str) -> PrSweepScanner | None:
    """Build a per-overlay PR-sweep scanner from the overlay's followup repos (#1257, #1309).

    Repo list comes from ``overlay.metadata.get_followup_repos()``. Returns
    ``None`` when the overlay has no Python class or no repos configured.
    ``solo_overlay`` opts the scanner into the single-author dogfood bypass
    (#1309) — a direct ``gh pr merge`` that skips the per-diff CLEAR — ONLY
    when the overlay's ``autonomy`` resolves to ``full`` (#1668). The
    ``notify`` tier collapses the same merge gates (``mode = auto`` +
    ``require_human_approval_to_merge = false``) but is a COLLABORATIVE
    surface: it must keep the CLEAR path so the user's MR merges only after a
    colleague approval and the agent never self-approves its own MR. Gating
    on the resolved ``autonomy`` (not the collapsed gate values) is what keeps
    the bypass exclusive to ``full``.
    """
    overlay = backend.overlay
    if overlay is None:
        return None
    repos = tuple(overlay.metadata.get_followup_repos())
    if not repos:
        return None
    github_token = overlay.config.get_github_token() if hasattr(overlay.config, "get_github_token") else ""
    notifier: SlackMergeNotifier | NullMergeNotifier
    if backend.messaging is not None and slack_user_id:
        notifier = SlackMergeNotifier(backend=backend.messaging, user_id=slack_user_id)
    else:
        notifier = NullMergeNotifier()
    settings = _effective_settings_for_overlay(backend.name)
    solo_overlay = settings.autonomy is Autonomy.FULL
    return PrSweepScanner(
        repos=repos,
        api=GhPrApiClient(token=github_token),
        keystone=CallCommandMergeKeystone(),
        notifier=notifier,
        overlay=backend.name,
        solo_overlay=solo_overlay,
    )


def _pull_main_clone_scanner_for(backend: OverlayBackends) -> PullMainCloneScanner | None:
    """Build a per-overlay pull-main-clone scanner from the overlay's workspace repos.

    Repo list comes from ``overlay.get_workspace_repos()``; each name is
    resolved to its on-disk main clone under ``$T3_WORKSPACE_DIR`` via
    :func:`teatree.core.clone_paths.find_clone_path` (the same namespace-
    aware resolver provisioning/cleanup use). A repo with no clone on disk
    is dropped — there is nothing to pull. The marker/signal label is
    namespaced ``"<overlay>:<repo>"`` so two overlays that share a repo
    basename keep independent cadence ledgers.

    Returns ``None`` when the overlay has no Python class, when
    ``pull_main_clone_disabled = true`` (the escape hatch), or when no
    workspace repo resolves to a clone.
    """
    overlay = backend.overlay
    if overlay is None:
        return None
    settings = _effective_settings_for_overlay(backend.name)
    if settings.pull_main_clone_disabled:
        return None
    workspace = workspace_dir()
    repos: list[tuple[str, Path]] = []
    for repo_name in overlay.get_workspace_repos():
        clone = find_clone_path(workspace, repo_name)
        if clone is None:
            continue
        repos.append((f"{backend.name}:{repo_name}", clone))
    if not repos:
        return None
    return PullMainCloneScanner(
        repos=tuple(repos),
        cadence_hours=settings.pull_main_clone_cadence_hours,
    )


def _codex_review_scanner_for(backend: OverlayBackends) -> CodexReviewScanner | None:
    """Build a per-overlay codex-review scanner from the overlay's followup repos (#1254).

    The fleet-of-agents doctrine ("auto-codex-on-every-push") only
    applies when the user has opted the overlay into end-to-end
    autonomy: ``mode = "auto"`` AND ``require_human_approval_to_merge =
    false``. On every other overlay the scanner is silent — the user is
    keeping a human-in-the-loop training wheel and explicit codex
    invocation stays manual.

    Repo list comes from ``overlay.metadata.get_followup_repos()``
    (same source as :class:`PrSweepScanner`). Returns ``None`` when the
    overlay has no Python class, no repos, or has not opted into the
    fleet doctrine.
    """
    overlay = backend.overlay
    if overlay is None:
        return None
    repos = tuple(overlay.metadata.get_followup_repos())
    if not repos:
        return None
    settings = _effective_settings_for_overlay(backend.name)
    if settings.mode != Mode.AUTO or settings.require_human_approval_to_merge:
        return None
    github_token = overlay.config.get_github_token() if hasattr(overlay.config, "get_github_token") else ""
    return CodexReviewScanner(
        repos=repos,
        api=GhCodexPrApi(token=github_token),
        overlay=backend.name,
    )


def _todo_sweep_scanner_for(backend: OverlayBackends) -> TodoSweepScanner | None:
    """Build a per-overlay TODO-sweep scanner (#129).

    Verifies open Task rows against their artifact's terminal state via the
    overlay's ``is_issue_done`` hook. Returns ``None`` when the overlay has no
    Python class (the scanner needs the overlay object as its terminal-state
    oracle) or when ``todo_sweep_disabled = true`` (the escape hatch). The
    per-task recheck/idempotency window comes from
    ``todo_sweep_recheck_interval_hours``.
    """
    overlay = backend.overlay
    if overlay is None:
        return None
    settings = _effective_settings_for_overlay(backend.name)
    if settings.todo_sweep_disabled:
        return None
    return TodoSweepScanner(
        overlay=overlay,
        overlay_name=backend.name,
        recheck_interval_hours=settings.todo_sweep_recheck_interval_hours,
    )


def _architectural_review_scanner_for(backend: OverlayBackends) -> ArchitecturalReviewScanner | None:
    """Build a per-overlay architectural-review scanner from teatree-core config.

    #1136 / #1152 re-architecture: the architectural-review cadence is a
    teatree-core platform behaviour that applies uniformly to every
    overlay's worktrees, NOT a per-overlay opt-in. The settings live on
    :class:`teatree.config.UserSettings` (the ``[teatree]`` table in
    ``~/.teatree.toml``, with optional per-overlay overrides via the
    standard ``[overlays.<name>]`` shape — see
    ``OVERLAY_OVERRIDABLE_SETTINGS``). The scanner is instantiated once
    per registered overlay so each overlay's task queue gets its own
    cadence; a single core ``architectural_review_disabled = true``
    escape hatch suppresses scanning for the active overlay (and an
    overlay-scoped override allows pinning the toggle per-overlay).

    Returns ``None`` when the active overlay has
    ``architectural_review_disabled = true`` (the escape hatch).
    Unlike the previous wiring, this no longer skips overlays without a
    Python class — the scanner only needs ``backend.name`` to operate.
    """
    settings = _effective_settings_for_overlay(backend.name)
    if settings.architectural_review_disabled:
        return None
    return ArchitecturalReviewScanner(
        overlay_name=backend.name,
        skill=settings.architectural_review_skill,
        cadence_hours=settings.architectural_review_cadence_hours,
        after_merge_count=settings.architectural_review_after_merge_count,
    )


def _issue_implementer_scanner_for(backend: OverlayBackends) -> IssueImplementerScanner | None:
    """Build a per-overlay issue-implementer scanner behind the triple gate (#1553).

    Returns a scanner ONLY when the always-on issue-implementer loop is
    opted in for this overlay AND the in-flight budget has room. Two of the
    triple gate's three checks live here; the third lives in the scanner.

    The master gate is ``issue_implementer_enabled`` (default False) — the
    loop is a hard no-op until an overlay flips it on. The concurrency gate
    is ``ImplementedIssueMarker.in_flight_count(overlay) <
    issue_implementer_max_concurrent`` — a full budget emits no scanner, so
    no further issue is picked up this tick.

    The third gate — per-issue claim idempotency — lives inside the scanner
    itself (:meth:`ImplementedIssueMarker.claim` returns ``None`` for an
    already-claimed issue, which the scanner skips).

    Returns ``None`` (no job emitted) whenever either gate is shut, so with
    the default-OFF config neither ``build_registry_jobs`` nor
    ``build_default_jobs`` emits anything for this domain — the registry
    fan-out stays byte-for-byte unchanged until an overlay opts in.

    A loop that is enabled with an empty ``issue_implementer_label`` is a
    safe but silent no-op (the scanner short-circuits on a blank label so no
    issue is ever claimed). That fails closed by design, but an operator who
    flipped the master gate without setting a label sees nothing dispatch and
    no reason why — so we emit one WARNING naming the missing label (#1554).
    """
    settings = _effective_settings_for_overlay(backend.name)
    if not settings.issue_implementer_enabled:
        return None
    if not settings.issue_implementer_label:
        logger.warning(
            "issue-implementer loop enabled for overlay %r but issue_implementer_label is empty — "
            "nothing will be dispatched until a label is set",
            backend.name,
        )
        return None
    code_host = backend.host
    if code_host is None:
        return None
    if ImplementedIssueMarker.objects.in_flight_count(backend.name) >= settings.issue_implementer_max_concurrent:
        return None
    return IssueImplementerScanner(
        host=code_host,
        label=settings.issue_implementer_label,
        overlay_name=backend.name,
        identities=backend.identities,
    )


#: Canonical fallback overlay anchor (#1267 / migration 0027). The
#: bundled teatree overlay registers via the ``teatree.overlays`` entry
#: point under this name; ``discover_active_overlay()`` resolves it in
#: ordinary installations. The literal here is a defensive default for
#: machines with no overlay registered — it is not consulted by the
#: scanner itself, which only ever sees the resolved string.
_CANONICAL_CORE_OVERLAY = "t3-teatree"


def _dogfood_smoke_scanner() -> Scanner | None:
    """Wire the global provision-smoke scanner (#1308)."""
    from teatree.loop.scanners.provision_smoke import build_provision_smoke_scanner  # noqa: PLC0415

    return build_provision_smoke_scanner(
        load_config=load_config,
        discover_active_overlay=discover_active_overlay,
        canonical_fallback=_CANONICAL_CORE_OVERLAY,
    )


def _collect_self_update_repos() -> list[tuple[str, Path]]:
    """Enumerate editable clones the self-update scanner should fast-forward (#1249).

    Returns ``(label, repo_path)`` pairs for the editable-installed
    teatree core clone plus every overlay clone discovered via
    :func:`teatree.config.discover_overlays`. The label is the human-
    friendly tag the scanner persists in :class:`SelfUpdateMarker`;
    ``"teatree"`` for core, the overlay's registered name for overlays.

    Targets stay in lockstep with what ``t3 update`` would touch: the
    teatree core clone first, then each overlay's ``project_path``
    resolved to its git toplevel. A repo wins exactly once even when
    two paths resolve to the same toplevel.
    """
    repos: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    core = _resolve_t3_repo()
    if core is not None:
        repos.append(("teatree", core))
        seen.add(core)

    for entry in discover_overlays():
        if entry.project_path is None:
            continue
        toplevel = _git_toplevel(entry.project_path.expanduser())
        if toplevel is None or toplevel in seen:
            continue
        seen.add(toplevel)
        repos.append((entry.name, toplevel))
    return repos


def _resolve_t3_repo() -> Path | None:
    """Resolve the editable teatree clone path from the ``T3_REPO`` env var.

    Returns ``None`` when the env var is unset, points at a missing
    directory, or points at a directory that does not look like a
    teatree clone (no ``pyproject.toml`` + ``.git``). Worktrees still
    qualify — ``.git`` may be a file pointing at the main clone's
    object store, which is the same shape ``t3 update`` handles.
    """
    env_path = os.environ.get("T3_REPO", "")
    if not env_path:
        return None
    candidate = Path(env_path).expanduser()
    if not (candidate / "pyproject.toml").is_file():
        return None
    git_entry = candidate / ".git"
    if not (git_entry.is_dir() or git_entry.is_file()):
        return None
    return candidate.resolve()


def _git_toplevel(path: Path) -> Path | None:
    """Return the git work-tree root containing *path*, or ``None`` if not a repo."""
    from teatree.utils.run import run_allowed_to_fail  # noqa: PLC0415

    if not path.is_dir():
        return None
    result = run_allowed_to_fail(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        expected_codes=None,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _self_update_scanner() -> SelfUpdateScanner | None:
    """Build the global self-update scanner from teatree-core config (#1249, #1760).

    Returns ``None`` when ``self_update_disabled = true`` (the escape
    hatch) OR when there are no editable clones to walk (a non-editable
    install with no registered overlay project paths — nothing to pull).
    Otherwise builds a single global :class:`SelfUpdateScanner` whose
    cadence honours the ``self_update_cadence_hours`` setting (default
    1 hour). The scanner is wired as a global job (``overlay=""``)
    because it concerns the editable installs themselves, not any one
    overlay's tracked work.

    #1760 wires the CI-green fail-closed gate and the deferred-reinstall
    queue: ``auto_update_require_green_main`` (default ON) refuses a
    ff-pull unless the default branch's CI is explicitly green — the
    verdict comes from :class:`GhMainCiStatus`, the same ``gh
    check-runs`` source the PR sweep uses. ``auto_update_reinstall``
    (default OFF, ``T3_LOOP_AUTO_UPDATE`` env wins) opts into queuing a
    deferred reinstall on an actual update.
    """
    settings = load_config().user
    if settings.self_update_disabled:
        return None
    repos = _collect_self_update_repos()
    if not repos:
        return None
    return SelfUpdateScanner(
        repos=tuple(repos),
        cadence_hours=settings.self_update_cadence_hours,
        ci_status=GhMainCiStatus(),
        require_green_main=settings.auto_update_require_green_main,
        auto_update_reinstall=settings.auto_update_reinstall,
    )


def _resource_pressure_scanner() -> ResourcePressureScanner | None:
    """Build the global resource-pressure scanner from teatree-core config (#128).

    Returns ``None`` when ``resource_pressure_disabled = true`` (the durable
    kill-switch, mirroring ``self_update_disabled``) so the job is never wired.
    Otherwise builds a single global :class:`ResourcePressureScanner`
    (``overlay=""``) — disk/RAM pressure is a host-level concern, not any one
    overlay's tracked work. All thresholds, cadence, allow-lists, and
    destructive opt-in flags come straight from ``UserSettings``; the
    destructive levers default OFF.
    """
    settings = load_config().user
    if settings.resource_pressure_disabled:
        return None
    return ResourcePressureScanner(
        disk_warn_free_gb=settings.disk_warn_free_gb,
        disk_crit_free_gb=settings.disk_crit_free_gb,
        ram_warn_avail_gb=settings.ram_warn_avail_gb,
        ram_crit_avail_gb=settings.ram_crit_avail_gb,
        cadence_minutes=settings.resource_pressure_cadence_minutes,
        min_free_interval_minutes=settings.resource_pressure_min_free_interval_minutes,
        disk_cache_allowlist=tuple(settings.disk_cache_allowlist),
        allow_destructive_disk=settings.allow_destructive_disk,
        worktree_stale_days=settings.worktree_stale_days,
        max_worktree_gc_per_tick=settings.max_worktree_gc_per_tick,
        allow_destructive_ram=settings.allow_destructive_ram,
        ram_kill_allowlist=tuple(settings.ram_kill_allowlist),
    )


def _scanning_news_scanner() -> ScanningNewsScanner | None:
    """Build a global scanning-news scanner from teatree-core config.

    #1191: the news-scan cadence is a teatree-core platform behaviour
    that runs once per day regardless of which overlays are registered.
    The settings live on :class:`teatree.config.UserSettings` (the
    ``[teatree]`` table in ``~/.teatree.toml``, with optional per-overlay
    overrides). Returns ``None`` when ``scanning_news_disabled = true``
    (the escape hatch).

    #1267: the overlay-anchor identity is resolved via
    :func:`teatree.config.discover_active_overlay` rather than baked
    into the scanner module. Falls back to the canonical post-0027
    overlay name (``t3-teatree``) when no overlay is registered.

    #1391: ``ask_before_creating_news_tickets`` (default true) is the
    ask-gate flag threaded into the scanner so the queued task instructs
    the skill to record candidates for approval instead of auto-filing
    issues.
    """
    settings = load_config().user
    if settings.scanning_news_disabled:
        return None
    active = discover_active_overlay()
    overlay_name = active.name if active is not None else _CANONICAL_CORE_OVERLAY
    return ScanningNewsScanner(
        overlay_name=overlay_name,
        skill=settings.scanning_news_skill,
        cadence_hours=settings.scanning_news_cadence_hours,
        require_approval=settings.ask_before_creating_news_tickets,
    )


def _eval_local_scanner() -> EvalLocalScanner | None:
    """Build a global local-eval scanner from teatree-core config.

    User directive (2026-06-05): "AI evals should be run locally from
    time to time, and in CI once a week." The CI half lives in
    ``.github/workflows/ci.yml`` (``eval-weekly``); this is the local
    half. The cadence is a teatree-core platform behaviour (weekly by
    default), so the settings live on :class:`teatree.config.UserSettings`
    (the ``[teatree]`` table, per-overlay overridable). Returns ``None``
    when ``eval_local_disabled = true`` (the escape hatch).

    The overlay-anchor identity is resolved via
    :func:`teatree.config.discover_active_overlay`, falling back to the
    canonical post-0027 overlay name (``t3-teatree``) when no overlay is
    registered — mirroring :func:`_scanning_news_scanner`.
    """
    settings = load_config().user
    if settings.eval_local_disabled:
        return None
    active = discover_active_overlay()
    overlay_name = active.name if active is not None else _CANONICAL_CORE_OVERLAY
    return EvalLocalScanner(
        overlay_name=overlay_name,
        skill=settings.eval_local_skill,
        cadence_hours=settings.eval_local_cadence_hours,
    )


def _effective_settings_for_overlay(overlay_name: str) -> "UserSettings":
    """Resolve :class:`UserSettings` for *overlay_name*, autonomy collapse applied.

    Thin wrapper over :func:`teatree.config.get_effective_settings` resolving a
    NAMED overlay — the scanner-builders fan out over every registered overlay,
    so they resolve by name rather than via ``T3_OVERLAY_NAME``. Routing through
    that resolver (not a bare ``replace``) is what makes the ``autonomy``
    collapse (#1668) visible to the loop's auto-merge / codex consumers;
    skipping it left a ``full``/``notify`` overlay's merge autonomy a silent
    no-op in the loop. Kept as a module-local indirection so the existing call
    sites and the builder tests that patch this name stay unchanged.
    """
    return get_effective_settings(overlay_name)


def _gitlab_approvals_enabled() -> bool:
    """Read the ``TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED`` feature flag.

    Default off — the scanner is poll-driven and overlaps with the webhook
    path; deployments that already wire ``/hooks/gitlab/`` do not need it.
    Returns True for any truthy value (``1``, ``true``, ``yes``,
    case-insensitive); anything else (unset, ``0``, ``false``) is off.
    """
    raw = os.environ.get("TEATREE_GITLAB_APPROVAL_SCANNER_ENABLED", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _run_job(job: _ScannerJob) -> tuple[str, list[ScanSignal], str]:
    label = f"{job.scanner.name}[{job.overlay}]" if job.overlay else job.scanner.name
    try:
        signals = job.scanner.scan()
        if job.overlay:
            signals = [
                ScanSignal(
                    kind=s.kind,
                    summary=s.summary,
                    payload={**s.payload, "overlay": job.overlay},
                )
                for s in signals
            ]
    except ScannerError as exc:
        # Auth / rate-limit / missing-scope / network: surface as a
        # structured error and DM the user once per day per
        # ``(scanner, error_class)`` so a sustained failure does not
        # spam the channel (#1287). The dispatcher continues with the
        # other scanners — only THIS scanner is skipped for one tick.
        logger.warning("Scanner %s recoverable error: %s", label, exc)
        _notify_scanner_error(label=label, exc=exc, overlay=job.overlay)
        return label, [], f"ScannerError[{exc.error_class.value}]: {exc.detail or exc}"
    except Exception as exc:
        logger.exception("Scanner %s raised", label)
        return label, [], f"{type(exc).__name__}: {exc}"
    return label, signals, ""


def _notify_scanner_error(*, label: str, exc: ScannerError, overlay: str) -> None:
    """DM the user that a scanner is degraded — once per day per class (#1287).

    Idempotency key is ``scanner_error:<scanner>:<error_class>:<utc-date>``
    so :func:`teatree.notify.notify_user`'s ``BotPing`` ledger dedups
    repeat ticks of the same failure inside one UTC day. The next day
    re-notifies — if the issue is still there, the user wants the
    reminder; if it cleared, no DM goes out.

    Best-effort: any failure inside the notify path is logged and
    swallowed so a notify failure never reverberates into the tick.
    """
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    key = f"scanner_error:{exc.scanner}:{exc.error_class.value}:{today}"
    overlay_tag = f" [overlay={overlay}]" if overlay else ""
    text = (
        f":warning: scanner *{exc.scanner}* hit *{exc.error_class.value}*"
        f"{overlay_tag} — this scanner is skipped for one tick."
    )
    if exc.detail:
        text = f"{text}\n_{exc.detail}_"
    try:
        notify_with_fallback(text, kind=NotifyKind.INFO, idempotency_key=key)
    except Exception:
        logger.exception("Scanner-error notify_with_fallback failed for %s", label)


def _user_slack_id_for_overlay(overlay_name: str) -> str:
    """Resolve ``slack_user_id`` for the active overlay (overlay → global → empty).

    Used by :class:`ReviewNagScanner` to know where to DM long-stale MR
    warnings. Reads ``~/.teatree.toml`` directly so a fresh tick picks up
    a runtime config change without requiring an overlay reload.
    """
    try:
        toml_path = Path.home() / ".teatree.toml"
        if not toml_path.is_file():
            return ""
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    overlays = data.get("overlays") or {}
    if overlay_name and isinstance(overlays.get(overlay_name), dict):
        user_id = overlays[overlay_name].get("slack_user_id", "")
        if user_id:
            return str(user_id)
    teatree_cfg = data.get("teatree") or {}
    return str(teatree_cfg.get("slack_user_id", ""))


def _user_identity_aliases_for_overlay(overlay_name: str) -> tuple[str, ...]:
    """Resolve ``user_identity_aliases`` honouring any per-overlay override.

    The active overlay's ``[overlays.<name>]`` table wins over the global
    ``[teatree]`` value; with no setting anywhere we return the empty
    tuple so the disposition scanner keeps its legacy behaviour.
    """
    try:
        global_value = tuple(load_config().user.user_identity_aliases)
        if overlay_name:
            for entry in discover_overlays():
                if entry.name == overlay_name:
                    override = entry.overrides.get("user_identity_aliases")
                    if override is not None:
                        return tuple(str(s) for s in override)
                    break
    except Exception:  # noqa: BLE001 — never break a tick on a config read.
        logger.warning("Failed to resolve user_identity_aliases for %r; defaulting to empty", overlay_name)
        return ()
    return global_value


def _global_dispatch_jobs() -> list[_ScannerJob]:
    """The always-on global triad ``build_default_jobs`` fans out once per tick."""
    return [
        _ScannerJob(scanner=PendingTasksScanner(), overlay=""),
        _ScannerJob(scanner=IncomingEventsScanner(), overlay=""),
        _ScannerJob(scanner=OutboundAuditScanner(), overlay=""),
    ]


def _tickets_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Local Ticket-DB scanners + per-host disposition/completion + TODO sweep."""
    tag = backend.name
    jobs: list[_ScannerJob] = []
    if backend.external_db is not None:
        from teatree.loop.scanners.external_tickets import ExternalTicketsScanner  # noqa: PLC0415

        jobs.append(
            _ScannerJob(
                scanner=ExternalTicketsScanner(overlay_name=tag, db_path=backend.external_db),
                overlay=tag,
            ),
        )
    else:
        jobs.append(_ScannerJob(scanner=ActiveTicketsScanner(overlay_name=tag), overlay=tag))
    jobs.append(
        _ScannerJob(
            scanner=StaleTicketsScanner(overlay_name=tag, threshold_days=backend.stale_threshold_days),
            overlay=tag,
        ),
    )
    jobs.extend(_tickets_per_host_jobs(backend, tag))
    todo_sweep_scanner = _todo_sweep_scanner_for(backend)
    if todo_sweep_scanner is not None:
        jobs.append(_ScannerJob(scanner=todo_sweep_scanner, overlay=tag))
    return jobs


def _tickets_per_host_jobs(backend: OverlayBackends, tag: str) -> list[_ScannerJob]:
    """Per-host disposition scanner + the once-per-overlay completion scanner.

    ``identity_groups`` is resolved only when there is a host to scan —
    the resolution reads the overlay config, so a host-less backend stays
    out of that path entirely.
    """
    if not backend.hosts:
        return []
    identity_groups = _identity_groups_for_overlay(backend)
    jobs: list[_ScannerJob] = []
    ticket_completion_emitted = False
    for code_host in backend.hosts:
        jobs.append(
            _ScannerJob(
                scanner=TicketDispositionScanner(
                    host=code_host,
                    overlay=backend.overlay,
                    ready_labels=backend.ready_labels,
                    overlay_name=tag,
                    user_identity_aliases=_user_identity_aliases_for_overlay(tag),
                    identity_alias_groups=identity_groups,
                ),
                overlay=tag,
            ),
        )
        if backend.overlay is not None and not ticket_completion_emitted:
            jobs.append(
                _ScannerJob(
                    scanner=TicketCompletionScanner(overlay=backend.overlay, overlay_name=tag),
                    overlay=tag,
                ),
            )
            ticket_completion_emitted = True
    return jobs


def _ship_jobs_for_overlay(
    backend: OverlayBackends,
    *,
    all_backends: tuple[OverlayBackends, ...],
) -> list[_ScannerJob]:
    """Own-author PR scanner + (opt-in) GitLab-approvals poll, per host."""
    tag = backend.name
    gitlab_approvals_enabled = _gitlab_approvals_enabled()
    jobs: list[_ScannerJob] = []
    for code_host in backend.hosts:
        url_prefixes = _allowed_url_prefixes_for_host(backend, code_host)
        competing_prefixes = _competing_url_prefixes(
            this_backend=backend,
            code_host=code_host,
            all_backends=all_backends,
        )
        jobs.append(
            _ScannerJob(
                scanner=MyPrsScanner(
                    host=code_host,
                    identities=backend.identities,
                    allowed_url_prefixes=url_prefixes,
                    competing_url_prefixes=competing_prefixes,
                ),
                overlay=tag,
            ),
        )
        if gitlab_approvals_enabled:
            jobs.append(
                _ScannerJob(
                    scanner=GitLabApprovalsScanner(host=code_host, identities=backend.identities),
                    overlay=tag,
                ),
            )
    return jobs


def _review_jobs_for_overlay(
    backend: OverlayBackends,
    *,
    all_backends: tuple[OverlayBackends, ...],
) -> list[_ScannerJob]:
    """Reviewer-PR (per host) + broadcast / codex / PR-sweep companions."""
    tag = backend.name
    jobs: list[_ScannerJob] = []
    for code_host in backend.hosts:
        url_prefixes = _allowed_url_prefixes_for_host(backend, code_host)
        competing_prefixes = _competing_url_prefixes(
            this_backend=backend,
            code_host=code_host,
            all_backends=all_backends,
        )
        jobs.append(
            _ScannerJob(
                scanner=ReviewerPrsScanner(
                    host=code_host,
                    identities=backend.identities,
                    overlay_name=tag,
                    allowed_url_prefixes=url_prefixes,
                    competing_url_prefixes=competing_prefixes,
                ),
                overlay=tag,
            ),
        )
    sweep_scanner = _pr_sweep_scanner_for(backend, slack_user_id=_user_slack_id_for_overlay(tag))
    if sweep_scanner is not None:
        jobs.append(_ScannerJob(scanner=sweep_scanner, overlay=tag))
    codex_scanner = _codex_review_scanner_for(backend)
    if codex_scanner is not None:
        jobs.append(_ScannerJob(scanner=codex_scanner, overlay=tag))
    broadcasts_scanner = _slack_broadcasts_scanner_for(backend)
    if broadcasts_scanner is not None:
        jobs.append(_ScannerJob(scanner=broadcasts_scanner, overlay=tag))
    return jobs


def _followup_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Assigned-issue intake (per host) + the single review-nag (overlay-scoped)."""
    tag = backend.name
    jobs: list[_ScannerJob] = [
        _ScannerJob(
            scanner=AssignedIssuesScanner(
                host=code_host,
                ready_labels=backend.ready_labels,
                exclude_labels=backend.exclude_labels,
                auto_start=backend.auto_start_assigned_issues,
                max_concurrent=backend.max_concurrent_auto_starts,
                overlay_name=tag,
                identities=backend.identities,
            ),
            overlay=tag,
        )
        for code_host in backend.hosts
    ]
    if backend.messaging is not None:
        jobs.extend(
            (
                _ScannerJob(
                    scanner=ReviewNagScanner(
                        messaging=backend.messaging,
                        user_slack_id=_user_slack_id_for_overlay(tag),
                        host=backend.host,
                        identities=backend.identities,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=ReviewRequestMergeReactScanner(
                        messaging=backend.messaging,
                        host=backend.host,
                        identities=backend.identities,
                    ),
                    overlay=tag,
                ),
            ),
        )
    return jobs


def _inbox_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Inbound Slack scanners (mentions/DM/review-intent/red-card), sans review-nag."""
    if backend.messaging is None:
        return []
    return _messaging_jobs_for_backend(backend, backend.name, include_review_nag=False)


def _arch_review_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Periodic architectural-review scanner (core platform cadence)."""
    scanner = _architectural_review_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _audit_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Failed-E2E Slack-post scanner driven by overlay watchers (#1295 cap E)."""
    scanner = _failed_e2e_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _housekeeping_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Per-overlay pull-main-clone scanner (workspace-repo fast-forward)."""
    scanner = _pull_main_clone_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _issue_implementer_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Per-overlay issue-implementer scanner behind the default-OFF triple gate (#1553).

    Empty by default — :func:`_issue_implementer_scanner_for` returns
    ``None`` unless the overlay opts in and has in-flight budget — so this
    domain slice contributes nothing to either fan-out path until an overlay
    enables the loop, keeping the registry/legacy parity green.
    """
    scanner = _issue_implementer_scanner_for(backend)
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay=backend.name)]


def _identity_groups_for_overlay(backend: OverlayBackends) -> tuple[tuple[str, ...], ...]:
    """Resolve disposition identity-alias groups with the multi-identity self-group fallback (#1113)."""
    groups = _identity_alias_groups_for_overlay(backend.name, backend)
    if not groups and len(backend.identities) > 1:
        return (tuple(backend.identities),)
    return groups


type _OverlayDomainBuilder = Callable[[OverlayBackends], list[_ScannerJob]]
type _UrlAwareDomainBuilder = Callable[..., list[_ScannerJob]]

#: PR scanners thread sibling URL claims (#1324), so these two domains
#: take ``all_backends``; the rest are overlay-local.
_URL_AWARE_DOMAIN_BUILDERS: dict[Domain, _UrlAwareDomainBuilder] = {
    Domain.SHIP: _ship_jobs_for_overlay,
    Domain.REVIEW: _review_jobs_for_overlay,
}

_PER_OVERLAY_DOMAIN_BUILDERS: dict[Domain, _OverlayDomainBuilder] = {
    Domain.TICKETS: _tickets_jobs_for_overlay,
    Domain.FOLLOWUP: _followup_jobs_for_overlay,
    Domain.INBOX: _inbox_jobs_for_overlay,
    Domain.ARCH_REVIEW: _arch_review_jobs_for_overlay,
    Domain.AUDIT: _audit_jobs_for_overlay,
    Domain.HOUSEKEEPING: _housekeeping_jobs_for_overlay,
    Domain.ISSUE_IMPLEMENTER: _issue_implementer_jobs_for_overlay,
}


def jobs_for_domain(
    domain: Domain,
    backend: OverlayBackends | None = None,
    *,
    all_backends: tuple[OverlayBackends, ...] = (),
) -> list[_ScannerJob]:
    """Return the scanner-job slice *domain* owns (#1482).

    The public, typed seam the mini-loops consume in place of reaching
    into ``tick_jobs`` privates. The per-overlay members
    (:data:`PER_OVERLAY_DOMAINS`) partition :func:`_jobs_for_overlay_backend`
    — disjoint and exhaustive — and require *backend*. ``Domain.DISPATCH``
    is the global triad and ignores *backend* (it carries no per-overlay
    state), so callers with no overlay context pass none.

    *all_backends* threads sibling URL claims into the PR scanners so a
    less-specific claim yields to a more specific sibling (#1324).
    """
    if domain is Domain.DISPATCH:
        return _global_dispatch_jobs()
    if backend is None:
        msg = f"{domain} is a per-overlay domain and requires a backend"
        raise ValueError(msg)
    if domain in _URL_AWARE_DOMAIN_BUILDERS:
        return _URL_AWARE_DOMAIN_BUILDERS[domain](backend, all_backends=all_backends)
    return _PER_OVERLAY_DOMAIN_BUILDERS[domain](backend)


def _jobs_for_overlay_backend(
    backend: OverlayBackends,
    *,
    all_backends: tuple[OverlayBackends, ...] = (),
) -> list[_ScannerJob]:
    """Build every scanner job that fans out for one overlay backend.

    Provably the sum of every per-overlay domain slice — the partition
    invariant pinned by ``tests/teatree_loop/test_jobs_for_domain.py``.
    The fan-out order follows ``PER_OVERLAY_DOMAINS``; the live tick
    treats jobs as an unordered set, so grouping by domain is behaviour-
    equivalent to the previous interleaved order.

    *all_backends* is the full multi-overlay roster — threaded into the
    PR scanners for cross-overlay URL attribution (#1324).
    """
    jobs: list[_ScannerJob] = []
    for domain in PER_OVERLAY_DOMAINS:
        jobs.extend(jobs_for_domain(domain, backend, all_backends=all_backends))
    return jobs


def _failed_e2e_scanner_for(backend: OverlayBackends) -> Scanner | None:
    """Build a per-overlay failed-E2E scanner from overlay watchers (#1295 cap E)."""
    from teatree.loop.scanners.failed_e2e_posts import failed_e2e_scanner_for  # noqa: PLC0415

    return failed_e2e_scanner_for(backend)


def _messaging_jobs_for_backend(
    backend: OverlayBackends,
    tag: str,
    *,
    include_review_nag: bool = True,
) -> list[_ScannerJob]:
    """Per-overlay Slack scanners that need a resolved messaging backend.

    ``SlackMentionsScanner`` owns the JSONL drain and fans reaction
    events into the backend's reactions queue; ``SlackReviewIntentScanner``
    must run after it so the queue is populated for the same tick.
    Caller must check ``backend.messaging is not None`` before invoking;
    a defensive early-return keeps the type narrow without a bare
    ``assert``.

    ``include_review_nag`` lets a high-cadence caller (the inbox mini-loop)
    drop ``ReviewNagScanner`` so the nag is emitted by exactly one owner —
    the followup mini-loop, whose 10-minute cadence matches the legacy
    single emission. The legacy monolithic fan-out keeps the default.
    """
    messaging = backend.messaging
    if messaging is None:
        return []
    jobs = [
        _ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=tag),
        _ScannerJob(scanner=SlackDmInboundScanner(backend=messaging, overlay=tag), overlay=tag),
        _ScannerJob(scanner=SlackReviewIntentScanner(backend=messaging, overlay=tag), overlay=tag),
        # #1130 RED CARD detection — user's structural "fix it upstream"
        # signal. Runs alongside the review-intent scanner because both
        # drain reactions; this one only cares about ``:red_circle:`` /
        # ``:no_entry_sign:`` plus the literal phrase in DMs.
        _ScannerJob(scanner=RedCardScanner(backend=messaging, overlay=tag), overlay=tag),
    ]
    if include_review_nag:
        jobs.append(
            _ScannerJob(
                scanner=ReviewNagScanner(
                    messaging=messaging,
                    user_slack_id=_user_slack_id_for_overlay(tag),
                    host=backend.host,
                    identities=backend.identities,
                ),
                overlay=tag,
            ),
        )
    return jobs


def build_default_jobs(
    *,
    backends: list[OverlayBackends] | None = None,
    host: CodeHostBackend | None = None,
    messaging: MessagingBackend | None = None,
    notion_client: NotionLike | None = None,
    ready_labels: tuple[str, ...] = (),
) -> list[_ScannerJob]:
    """Build the default scanner jobs from one or more overlays.

    Pass *backends* to scan multiple overlays in one tick (each gets its
    own host/messaging credentials). The *host*/*messaging* shape
    is preserved for callers that resolve a single overlay themselves.
    """
    jobs: list[_ScannerJob] = jobs_for_domain(Domain.DISPATCH)
    # #1191 Periodic scanning-news scanner — teatree-CORE global (not
    # per-overlay). Daily cadence is teatree-platform config; the queued
    # task is anchored on the `teatree` overlay placeholder ticket so
    # the dispatcher routes through the standard pending-task pipeline.
    # #1191 / #1308 — global teatree-CORE scanners (news + provision smoke).
    jobs.extend(
        _ScannerJob(scanner=s, overlay="")
        for s in (_scanning_news_scanner(), _dogfood_smoke_scanner(), _eval_local_scanner())
        if s
    )
    # #1249 Self-update scanner — fast-forwards the editable teatree
    # core clone + every registered overlay clone to ``origin/<default>``
    # once the cadence has elapsed. Wired as a global job because it
    # concerns the editable installs themselves, not any one overlay's
    # tracked work.
    self_update_scanner = _self_update_scanner()
    if self_update_scanner is not None:
        jobs.append(_ScannerJob(scanner=self_update_scanner, overlay=""))
    # #128 Resource-pressure scanner — global (overlay="") host-level
    # disk/RAM auto-free. Monitoring + regenerable-cache purge on by
    # default; destructive levers flag-gated off. Kill-switch:
    # ``resource_pressure_disabled = true`` → builder returns None.
    resource_pressure_scanner = _resource_pressure_scanner()
    if resource_pressure_scanner is not None:
        jobs.append(_ScannerJob(scanner=resource_pressure_scanner, overlay=""))

    if backends:
        all_backends = tuple(backends)
        for backend in backends:
            jobs.extend(_jobs_for_overlay_backend(backend, all_backends=all_backends))
    else:
        if host is not None:
            jobs.extend(
                [
                    _ScannerJob(scanner=MyPrsScanner(host=host), overlay=""),
                    _ScannerJob(scanner=ReviewerPrsScanner(host=host), overlay=""),
                    _ScannerJob(scanner=AssignedIssuesScanner(host=host, ready_labels=ready_labels), overlay=""),
                ],
            )
        if messaging is not None:
            jobs.extend(
                [
                    _ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=""),
                    _ScannerJob(scanner=SlackDmInboundScanner(backend=messaging, overlay=""), overlay=""),
                    _ScannerJob(scanner=SlackReviewIntentScanner(backend=messaging, overlay=""), overlay=""),
                    # #1130 RED CARD detection for the single-overlay path.
                    _ScannerJob(scanner=RedCardScanner(backend=messaging, overlay=""), overlay=""),
                ]
            )

    if notion_client is not None:
        jobs.append(_ScannerJob(scanner=NotionViewScanner(client=notion_client), overlay=""))
    return jobs


def build_default_scanners(
    *,
    host: CodeHostBackend | None,
    messaging: MessagingBackend | None,
    notion_client: NotionLike | None = None,
    ready_labels: tuple[str, ...] = (),
) -> list[Scanner]:
    """Single-overlay scanner builder kept for tests and ad-hoc CLI use."""
    return [
        job.scanner
        for job in build_default_jobs(
            host=host,
            messaging=messaging,
            notion_client=notion_client,
            ready_labels=ready_labels,
        )
    ]
