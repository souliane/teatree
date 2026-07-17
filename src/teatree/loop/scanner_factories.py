"""Per-overlay scanner constructors + their config/identity helpers.

The ``_*_scanner_for`` builders and the host-fanout / identity / settings helpers
the per-overlay domain slices (``domain_jobs``) consume. Depends DOWN on
``job_identity``; reads effective settings + overlay discovery from
``teatree.config``. Carved out of the loop tick fan-out to stay under the module-health LOC cap.
"""

import logging
from typing import TYPE_CHECKING

from teatree.config import (
    Autonomy,
    Mode,
    UserSettings,
    clone_root,
    effective_trusted_issue_authors,
    get_effective_settings,
)
from teatree.core.backend_factory import OverlayBackends
from teatree.core.merge import normalize_repo_slug
from teatree.core.models import ImplementedIssueMarker
from teatree.core.worktree.clone_paths import find_clone_path
from teatree.loop.job_identity import _TUPLE_PAIR
from teatree.loop.scanner_host_fanout import _competing_url_prefixes, _jobs_for_backend_hosts
from teatree.loop.scanners import (
    ArchitecturalReviewScanner,
    AutoReviewTaskDispatcher,
    BackendChannelHistoryFetcher,
    CallCommandMergeKeystone,
    CodexReviewScanner,
    GhCodexPrApi,
    GhPrApiClient,
    GlabGhMrStateClassifier,
    IssueDispositionScanner,
    IssueImplementerScanner,
    NullMergeNotifier,
    PrSweepScanner,
    PullMainCloneScanner,
    SlackBroadcastsScanner,
    SlackMergeNotifier,
    TaskSweepScanner,
    TriageAssessorScanner,
)
from teatree.loop.substrate_pinger import NotifyWithFallbackSubstratePinger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

# Re-exported for ``tick`` / ``domain_jobs`` / the builder tests, which import the
# host fan-out from this module; its body lives in ``scanner_host_fanout`` (#3235).
__all__ = ["_competing_url_prefixes", "_jobs_for_backend_hosts"]


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
    MR-link broadcasts so a reviewer-role tag in a Slack-Connect review team
    triggers the same downstream dispatch as a direct ``:eyes:``
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
    glab_token = overlay.config.get_gitlab_token()
    github_token = overlay.config.get_github_token()
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
    github_token = overlay.config.get_github_token()
    notifier: SlackMergeNotifier | NullMergeNotifier
    if backend.messaging is not None and slack_user_id:
        notifier = SlackMergeNotifier(backend=backend.messaging, user_id=slack_user_id)
    else:
        notifier = NullMergeNotifier()
    settings = _effective_settings_for_overlay(backend.name)
    solo_overlay = settings.autonomy is Autonomy.FULL
    # #68: a green own PR with no independent verdict can't self-merge — arm the
    # cold-review dispatch so the loop closes the loop. Gated on the same posture
    # as the solo-overlay merge bypass (full autonomy) AND an explicit
    # require_human_approval_to_merge=false: a human-approval overlay keeps the
    # human in the merge loop, so the agent must not auto-dispatch its own review.
    auto_review_dispatch = solo_overlay and not settings.require_human_approval_to_merge
    return PrSweepScanner(
        repos=repos,
        api=GhPrApiClient(token=github_token),
        keystone=CallCommandMergeKeystone(),
        notifier=notifier,
        overlay=backend.name,
        solo_overlay=solo_overlay,
        auto_review_dispatch=auto_review_dispatch,
        review_dispatcher=AutoReviewTaskDispatcher() if auto_review_dispatch else None,
        # #2210: scope the review-arm to the operator's own PRs — a colleague's
        # open PR in a watched repo must never be auto-scheduled for review.
        self_identities=backend.identities,
        # Ping-and-hold: a held SUBSTRATE merge DMs the owner once (deduped per
        # diff via the BotPing ledger) so substrate is never auto-merged silently.
        substrate_pinger=NotifyWithFallbackSubstratePinger(),
    )


def _pull_main_clone_scanner_for(backend: OverlayBackends) -> PullMainCloneScanner | None:
    """Build a per-overlay pull-main-clone scanner from the overlay's workspace repos.

    Repo list comes from ``overlay.get_workspace_repos()``; each name is
    resolved to its on-disk main clone under the CLONE root
    (``config.clone_root()``, ``~/workspace``) via
    :func:`teatree.core.worktree.clone_paths.find_clone_path` (the same namespace-
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
    workspace = clone_root()
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
    github_token = overlay.config.get_github_token()
    return CodexReviewScanner(
        repos=repos,
        api=GhCodexPrApi(token=github_token),
        overlay=backend.name,
    )


def _task_sweep_scanner_for(backend: OverlayBackends) -> TaskSweepScanner | None:
    """Build a per-overlay task-sweep scanner (#129).

    Verifies open teatree Task rows against their artifact's terminal state via
    the overlay's ``is_issue_done`` hook. Returns ``None`` when the overlay has
    no Python class (the scanner needs the overlay object as its terminal-state
    oracle) or when ``task_sweep_disabled = true`` (the escape hatch). The
    per-task recheck/idempotency window comes from
    ``task_sweep_recheck_interval_hours``.
    """
    overlay = backend.overlay
    if overlay is None:
        return None
    settings = _effective_settings_for_overlay(backend.name)
    if settings.task_sweep_disabled:
        return None
    return TaskSweepScanner(
        overlay=overlay,
        overlay_name=backend.name,
        recheck_interval_hours=settings.task_sweep_recheck_interval_hours,
    )


def _architectural_review_scanner_for(backend: OverlayBackends) -> ArchitecturalReviewScanner | None:
    """Build a per-overlay architectural-review scanner from teatree-core config.

    #1136 / #1152 re-architecture: the architectural-review cadence is a
    teatree-core platform behaviour that applies uniformly to every
    overlay's worktrees, NOT a per-overlay opt-in. The settings live on
    :class:`teatree.config.UserSettings` (DB-home in the ``ConfigSetting``
    store, with optional per-overlay overrides via the
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


def _owned_repo_slugs(overlay: "OverlayBase | None") -> tuple[str, ...]:
    """The ``owner/name`` slugs of the repos this overlay works in — the intake scope.

    Unions the overlay's followup repos (where the factory files and picks up issues)
    with its declared merge-candidate working repos (e.g. an ``e2e`` companion), each
    normalized up to ``owner/repo``. An overlay with no repo declarations (or none at
    all) yields ``()`` — the scanner then keeps the pre-scope global author search.
    """
    if overlay is None:
        return ()
    slugs: list[str] = []
    for value in (*overlay.review.merge_candidate_repo_slugs(), *overlay.metadata.get_followup_repos()):
        slug = normalize_repo_slug(value)
        if slug and slug not in slugs:
            slugs.append(slug)
    return tuple(slugs)


def _issue_implementer_scanner_for(backend: OverlayBackends) -> IssueImplementerScanner | None:
    """Build a per-overlay issue-implementer scanner behind the triple gate (#1553, #3235).

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
    the default-OFF config neither ``build_loop_table_jobs`` nor
    ``build_default_jobs`` emits anything for this domain — the live
    fan-out stays byte-for-byte unchanged until an overlay opts in.

    #3235 — INTAKE BY TRUSTED AUTHOR. The builder resolves the CONFIG tier of the
    trusted-author set (:func:`~teatree.config.effective_trusted_issue_authors`: the
    owner's ``user_identity_aliases`` unioned with the ``trusted_issue_authors`` allowlist) and
    hands it to the scanner, which unions in the DB ``TrustedIdentity`` rows and
    enforces the fail-closed per-issue gate. An EMPTY label is therefore no longer a
    kill-switch: intake is by author, and the label only applies when the operator
    explicitly opts back into it with ``issue_implementer_require_label``. That flag
    WITH an empty label is a safe but silent no-op (nothing can ever match), so the
    operator who set up that contradiction gets one WARNING naming the missing label.

    Fleet-safety Stage 2: when ``fleet_claim_enabled`` is on the scanner is
    emitted even at a full budget (or a require-label contradiction) — with
    ``can_claim=False`` it claims nothing new but STILL runs the per-tick heartbeat
    sweep, so an in-flight claim can never expire and be stolen mid-dispatch. With the
    kill-switch OFF the emission stays byte-for-byte the pre-Stage-2 behaviour
    (no scanner unless we can actually claim).
    """
    from teatree.core.fleet import wire  # noqa: PLC0415 — leaf import kept out of module load

    settings = _effective_settings_for_overlay(backend.name)
    if not settings.issue_implementer_enabled:
        return None
    code_host = backend.host
    if code_host is None:
        return None
    # #3275: self-heal the in-flight budget BEFORE reading it. A marker orphaned
    # while the pipeline was down never leaves ``dispatched``/``ticket_created``
    # (release-on-completion only fires on the live transition event), so it
    # strands its slot and ``has_budget`` reads false forever. Reconciling each
    # tick releases terminal-ticket / gone-ticket markers so the gate below sees
    # a current count and intake never permanently jams.
    ImplementedIssueMarker.objects.reconcile_stale(backend.name)
    label_satisfiable = bool(settings.issue_implementer_label) or not settings.issue_implementer_require_label
    if not label_satisfiable:
        logger.warning(
            "issue-implementer loop enabled for overlay %r with issue_implementer_require_label=true but "
            "issue_implementer_label is empty — nothing will be dispatched until a label is set "
            "(or the require-label flag is turned off, which intakes by trusted author alone)",
            backend.name,
        )
    has_budget = (
        ImplementedIssueMarker.objects.in_flight_count(backend.name) < settings.issue_implementer_max_concurrent
    )
    can_claim = label_satisfiable and has_budget
    if not can_claim and not wire.fleet_claim_enabled(backend.name):
        return None
    return IssueImplementerScanner(
        host=code_host,
        label=settings.issue_implementer_label,
        overlay_name=backend.name,
        trusted_authors=tuple(sorted(effective_trusted_issue_authors(settings))),
        require_label=settings.issue_implementer_require_label,
        identities=backend.identities,
        repo_slugs=_owned_repo_slugs(backend.overlay),
        can_claim=can_claim,
        max_concurrent=settings.issue_implementer_max_concurrent,
    )


def _issue_disposition_scanner_for(backend: OverlayBackends) -> IssueDispositionScanner | None:
    """Build a per-overlay issue-disposition scanner behind the default-OFF gate (#2122).

    Returns a scanner ONLY when ``auto_disposition_enabled`` is flipped on for
    this overlay. With the default-OFF config no scanner is built, so neither
    ``build_loop_table_jobs`` nor ``build_default_jobs`` emits anything for this
    domain — the fan-out stays byte-for-byte unchanged until an overlay opts in.

    ``repo`` (the duplicate-search target) and the obsolescence ``path_exists``
    oracle both come from the overlay's repos: the first followup/workspace repo
    names the duplicate-search project, and a clone-relative resolver answers
    whether a body-referenced path still exists on disk. An overlay with no
    Python class — hence no repo list — still gets a scanner, but with the
    duplicate and obsolete buckets self-disabled (empty ``repo`` /
    ``path_exists=None``); only the already-shipped bucket (pure local-DB
    evidence) stays active, which is the safe conservative default.
    """
    settings = _effective_settings_for_overlay(backend.name)
    if not settings.auto_disposition_enabled:
        return None
    code_host = backend.host
    if code_host is None:
        return None
    overlay = backend.overlay
    repo = ""
    path_exists: Callable[[str], bool] | None = None
    if overlay is not None:
        repos = list(overlay.metadata.get_followup_repos()) or list(overlay.get_workspace_repos())
        repo = repos[0] if repos else ""
        path_exists = _clone_relative_path_exists(overlay.get_workspace_repos())
    return IssueDispositionScanner(
        host=code_host,
        repo=repo,
        overlay_name=backend.name,
        identities=backend.identities,
        max_closes_per_tick=settings.auto_disposition_max_closes_per_tick,
        path_exists=path_exists,
    )


def _triage_assessor_scanner_for(backend: OverlayBackends) -> TriageAssessorScanner | None:
    """Build a per-overlay triage-assessor scanner behind the default-OFF gate.

    Returns a scanner ONLY when ``triage_assessor_enabled`` is flipped on for this
    overlay. With the default-OFF config no scanner is built, so neither
    ``build_loop_table_jobs`` nor ``build_default_jobs`` emits anything for this
    domain — the fan-out stays byte-for-byte unchanged until an overlay opts in.

    ``None`` also when the overlay has no code host (nothing to list issues on).
    The cadence / per-tick bound / operator identities are threaded from effective
    settings; the scanner never writes to the host — it only queues an assessment
    task behind the ask-gate.
    """
    settings = _effective_settings_for_overlay(backend.name)
    if not settings.triage_assessor_enabled:
        return None
    code_host = backend.host
    if code_host is None:
        return None
    return TriageAssessorScanner(
        host=code_host,
        overlay_name=backend.name,
        identities=backend.identities,
        cadence_hours=settings.triage_assessor_cadence_hours,
        max_issues_per_tick=settings.triage_assessor_max_issues_per_tick,
    )


def _clone_relative_path_exists(workspace_repos: list[str]) -> "Callable[[str], bool] | None":
    """Resolve the obsolescence oracle: does *path* still exist under any clone?

    Returns ``None`` when no workspace repo resolves to an on-disk clone — with
    no clone to check against, the obsolete bucket must stay disabled rather than
    guess. Otherwise returns a predicate that is True when the relative *path*
    exists under at least one resolved clone.
    """
    workspace = clone_root()
    clones = [clone for name in workspace_repos if (clone := find_clone_path(workspace, name)) is not None]
    if not clones:
        return None

    def _exists(path: str) -> bool:
        return any((clone / path).exists() for clone in clones)

    return _exists


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
