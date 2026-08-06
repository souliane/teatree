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
    PrReviewBackend,
    UserSettings,
    clone_root,
    effective_trusted_issue_authors,
    get_effective_settings,
)
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.intake.budget import read_intake_budget
from teatree.core.intake.concurrency import resolve_intake_concurrency
from teatree.core.merge import normalize_repo_slug
from teatree.core.models import ImplementedIssueMarker
from teatree.core.review.pr_review_backend import resolve_pr_review_backend
from teatree.core.worktree.clone_paths import find_clone_path
from teatree.loop.job_identity import _TUPLE_PAIR
from teatree.loop.scanner_host_fanout import _competing_url_prefixes, _jobs_for_backend_hosts
from teatree.loop.scanners import (
    ArchitecturalReviewScanner,
    AutoReviewTaskDispatcher,
    BackendChannelHistoryFetcher,
    CallCommandMergeKeystone,
    ClaudeSelfPrReviewScanner,
    CodexReviewScanner,
    GhCodexPrApi,
    GhPrApiClient,
    GlabGhMrStateClassifier,
    IssueDispositionScanner,
    IssueIntakeScanner,
    MrConflictScanner,
    MrTriageScanner,
    NullMergeNotifier,
    PrSweepScanner,
    PullMainCloneScanner,
    SlackBroadcastsScanner,
    SlackMergeNotifier,
    TaskSweepScanner,
    TriageAssessorScanner,
)
from teatree.loop.scanners.review_nag import default_repo_owner
from teatree.loop.substrate_pinger import NotifyWithFallbackSubstratePinger
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host

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
        # #3413: the owner's standing substrate delegation, sourced from config.
        # Empty (the default) keeps substrate held-for-owner; a configured owner id
        # lets the sweep auto-merge a substrate PR that passes EVERY gate and DM the
        # owner "informed, not asked".
        substrate_standing_authorizer=settings.substrate_auto_merge_authorized_by,
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


def _admit_colleague_prs_to_board(overlay_name: str) -> bool:
    """#3569: whether COLLEAGUE / requested-reviewer PRs are admitted to the review board.

    Self-authored PRs are always admitted; colleague PRs only when this is ON (the
    default). The review intake builds :class:`ReviewerPrsScanner` only when true.
    """
    settings = _effective_settings_for_overlay(overlay_name)
    return settings.admit_colleague_prs_to_board


def _self_pr_review_scanner_for(backend: OverlayBackends) -> "ClaudeSelfPrReviewScanner | CodexReviewScanner | None":
    """Build the per-overlay SELF-authored-PR review scanner (#1254, #3569).

    Self-authored open PRs are ALWAYS admitted to the review board: this sweeps
    the owner's own open PRs and enqueues one review task per un-reviewed head SHA
    (per-SHA dedup = "since last review"). It is the SAME quality gate colleague
    PRs get — the review execution is blind to author.

    WHICH reviewer runs is ``pr_review_backend``
    (:func:`~teatree.core.review.pr_review_backend.resolve_pr_review_backend`): the
    Claude scanner routes to ``reviewing`` → ``t3:reviewer``, the codex one to
    ``codex_reviewing`` → ``/codex:review``. The setting picks the reviewer; it can
    never pick "nobody", so a self-PR is reviewed either way. Repo list comes from
    ``overlay.metadata.get_followup_repos()`` (same source as
    :class:`PrSweepScanner`). Returns ``None`` when the overlay has no Python class
    or no followup repos.
    """
    overlay = backend.overlay
    if overlay is None:
        return None
    repos = tuple(overlay.metadata.get_followup_repos())
    if not repos:
        return None
    api = GhCodexPrApi(token=overlay.config.get_github_token())
    if resolve_pr_review_backend(backend.name) is PrReviewBackend.CODEX:
        return CodexReviewScanner(repos=repos, api=api, overlay=backend.name)
    return ClaudeSelfPrReviewScanner(repos=repos, api=api, overlay=backend.name)


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
        retry_backoff_hours=settings.architectural_review_retry_backoff_hours,
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


def _reconcile_holder_pr_rows(overlay_name: str) -> None:
    """Ask the forge about each budget holder's PR before the budget is read (#3984).

    Both intake readings — the release rule and the deadlock alarm — are drawn from
    ``PullRequest.state``, so a row nobody advanced after its PR merged holds the slot
    AND silences the alarm about it. Best-effort: an unreadable forge leaves rows
    unsettled (the reader collapses every error to UNKNOWN, which never settles), and a
    failure here must not stop the tick claiming.
    """
    from teatree.backends.loader import pr_open_state  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.core.intake.budget import reconcile_holder_pr_rows  # noqa: PLC0415 — leaf import

    try:
        reconcile_holder_pr_rows(overlay_name, read_state=pr_open_state)
    except Exception:
        logger.exception(
            "intake: could not reconcile held PR rows for %s — reading the budget as recorded", overlay_name
        )


def _issue_intake_scanner_for(backend: OverlayBackends) -> IssueIntakeScanner | None:
    """Build the per-overlay unified intake scanner behind the triple gate (#3634).

    Returns a scanner ONLY when the intake loop is opted in for this overlay AND
    the in-flight budget has room. Two of the triple gate's three checks live
    here; the third — per-issue claim idempotency — lives in the scanner
    (:meth:`ImplementedIssueMarker.claim` returns ``None`` for an already-claimed
    issue).

    The master gate is ``issue_implementer_enabled``, ON since #3895, so the default
    config DOES emit this domain's job; flipping it off emits nothing at all.

    The builder resolves the CONFIG tier of the trusted-author set
    (:func:`~teatree.config.effective_trusted_issue_authors`) and the admit label
    (``issue_implementer_label``, falling back to the shipped
    :data:`~teatree.core.intake.factory_admission.DEFAULT_ADMIT_LABEL`); the scanner
    unions in the DB ``TrustedIdentity`` rows and applies the top-down decision table.

    The scanner is emitted at a FULL budget too, with ``can_claim=False``: it claims
    nothing, but it still runs the per-tick heartbeat sweep (an in-flight claim would
    otherwise expire and be stolen mid-dispatch) and still records the queue it cannot
    act on. Returning ``None`` here is what made starvation invisible — the forge was
    never asked, so an issue that never got a slot was never even seen (#4238).

    The in-flight LIMIT comes from :func:`resolve_intake_concurrency` (#3992), which
    hands back the resource loop's headroom-derived number, or
    ``issue_implementer_max_concurrent`` verbatim whenever that number is missing,
    stale, or switched off.
    """
    from teatree.core.admission_governor import (  # noqa: PLC0415 — leaf import
        MERGE_STUCK_AFTER_TICKS,
        read_merge_signal,
    )
    from teatree.core.intake.factory_admission import DEFAULT_ADMIT_LABEL  # noqa: PLC0415 — leaf import

    settings = _effective_settings_for_overlay(backend.name)
    if not settings.issue_implementer_enabled:
        return None
    code_host = backend.host
    if code_host is None:
        return None
    _reconcile_holder_pr_rows(backend.name)
    # #3275: self-heal the in-flight budget BEFORE reading it. A marker orphaned
    # while the pipeline was down never leaves ``dispatched``/``ticket_created``,
    # so it strands its slot and the budget gate reads false forever.
    ImplementedIssueMarker.objects.reconcile_stale(backend.name)
    limit = resolve_intake_concurrency(settings.issue_implementer_max_concurrent, overlay=backend.name)
    budget = read_intake_budget(backend.name, limit)
    can_claim = not budget.at_budget
    if not can_claim:
        # #3978: without this the tick returns None, does nothing and reports success —
        # enabled loop, advancing last-run stamp, no error, and no surface anywhere
        # saying intake is at budget and claiming nothing.
        logger.warning("%s", budget.report())
    if can_claim:
        # #4044: do not deepen a pile that cannot land. When every open PR is one the
        # merge sweep keeps refusing, the constraint is downstream and another claimed
        # issue cannot help — it only adds inventory. Claiming stops; the heartbeat
        # sweep below still runs so no in-flight claim expires, and the ship and review
        # lanes are untouched, so the work that CLEARS the pile keeps going. The brake
        # releases itself as soon as one PR starts moving again.
        merge = read_merge_signal(overlay=backend.name)
        if merge.stalled:
            can_claim = False
            logger.warning(
                "issue intake is claiming nothing new: %d of %d open PR(s) refused by the merge sweep "
                "%d+ consecutive times — clear the pipeline before adding to it",
                merge.stuck_prs,
                merge.open_prs,
                MERGE_STUCK_AFTER_TICKS,
            )
    return IssueIntakeScanner(
        host=code_host,
        admit_label=settings.issue_implementer_label or DEFAULT_ADMIT_LABEL,
        overlay_name=backend.name,
        trusted_authors=tuple(sorted(effective_trusted_issue_authors(settings))),
        identities=backend.identities,
        exclude_labels=backend.exclude_labels,
        repo_slugs=_owned_repo_slugs(backend.overlay),
        can_claim=can_claim,
        max_concurrent=limit,
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
    """Build a per-overlay triage-assessor scanner behind its master gate.

    Returns a scanner ONLY when ``triage_assessor_enabled`` is on for this overlay —
    ON since #3895. Flipped off, no scanner is built, so neither
    ``build_loop_table_jobs`` nor ``build_default_jobs`` emits anything for this
    domain and the fan-out is byte-for-byte the pre-#3895 one.

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


def _mr_triage_scanner_for(backend: OverlayBackends) -> MrTriageScanner | None:
    """Build a per-overlay MR-triage surveyor behind the default-OFF gate.

    Returns a scanner ONLY when ``mr_triage_enabled`` is flipped on for this overlay.
    With the default-OFF config no scanner is built, so neither ``build_loop_table_jobs``
    nor ``build_default_jobs`` emits anything for this domain — the fan-out stays
    byte-for-byte unchanged until an overlay opts in.

    ``None`` also when the overlay has no code host (no MRs to read). The nag-patience
    inputs are resolved from the same overlay hook the review nag uses, so the two can
    never disagree about how long a repo waits.
    """
    settings = _effective_settings_for_overlay(backend.name)
    if not settings.mr_triage_enabled:
        return None
    code_host = backend.host
    if code_host is None:
        return None
    overlay = backend.overlay
    return MrTriageScanner(
        host=code_host,
        overlay_name=backend.name,
        identities=backend.identities,
        repo_owner=overlay.review.repo_owner_for_slug if overlay is not None else default_repo_owner,
        max_mrs_per_tick=settings.mr_triage_max_mrs_per_tick,
    )


def _mr_conflict_scanner_for(backend: OverlayBackends, code_host: CodeHostBackend) -> MrConflictScanner | None:
    """Build the per-host merge-conflict sweep behind the default-OFF gate.

    Returns a scanner ONLY when ``mr_conflict_scan_enabled`` is flipped on for this
    overlay. With the default-OFF config none is built, so the fan-out is
    byte-for-byte what it was before the sweep existed — it ships inert.

    Per HOST rather than per overlay because the conflict probe is a forge call:
    it must go to the host that lists the merge request, and an overlay with both
    a GitHub and a GitLab credential lists on both.
    """
    settings = _effective_settings_for_overlay(backend.name)
    if not settings.mr_conflict_scan_enabled:
        return None
    return MrConflictScanner(
        host=code_host,
        identities=backend.identities,
        allowed_url_prefixes=_allowed_url_prefixes_for_host(backend, code_host),
        overlay_name=backend.name,
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
