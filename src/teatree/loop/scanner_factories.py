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
from teatree.core.intake.budget import read_intake_budget, release_deadlocked_holder
from teatree.core.intake.concurrency import resolve_intake_concurrency
from teatree.core.models import ImplementedIssueMarker
from teatree.core.overlay_repos import owned_repo_slugs
from teatree.core.review.pr_review_backend import resolve_pr_review_backend
from teatree.core.worktree.clone_paths import find_clone_path
from teatree.loop.job_identity import _TUPLE_PAIR, CANONICAL_CORE_OVERLAY
from teatree.loop.reconcile_lanes import reconcile_holder_pr_rows_best_effort, reconcile_settled_clears_best_effort
from teatree.loop.scanner_factory_broadcast_claims import (
    _own_author_identity,
    _review_taken_probe,
    _self_forge_identities,
)
from teatree.loop.scanner_factory_config import _user_identity_aliases_for_overlay
from teatree.loop.scanner_host_fanout import _competing_url_prefixes, _jobs_for_backend_hosts
from teatree.loop.scanners import (
    ArchitecturalReviewScanner,
    AutoReviewTaskDispatcher,
    BackendChannelHistoryFetcher,
    CallCommandMergeKeystone,
    ClaudeSelfPrReviewScanner,
    CodexReviewScanner,
    ForgePrApiClient,
    GhCodexPrApi,
    GhPrApiClient,
    GlabGhMrStateClassifier,
    GlabPrApiClient,
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
from teatree.loop.scanners.my_prs import CiEnricher
from teatree.loop.scanners.review_nag import default_repo_owner
from teatree.loop.substrate_pinger import NotifyWithFallbackSubstratePinger
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host

if TYPE_CHECKING:
    from pathlib import Path


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
    return SlackBroadcastsScanner(
        backend=backend.messaging,
        channels=channel_ids,
        fetch_channel_history=BackendChannelHistoryFetcher(backend=backend.messaging),
        classify_mrs=GlabGhMrStateClassifier(),
        overlay=backend.name,
        current_gitlab_username=_own_author_identity(backend),
        owner_identities=_user_identity_aliases_for_overlay(backend.name),
        review_taken=_review_taken_probe(overlay, _self_forge_identities(backend)),
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
    gitlab_token = overlay.config.get_gitlab_token()
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
    # #4250: spend the authorisations whose PR already settled before the sweep reads
    # the backlog, so the operator alarm converges to zero instead of standing forever.
    reconcile_settled_clears_best_effort()
    return PrSweepScanner(
        repos=repos,
        # #72: a bare slug carries no host, so routing per slug is what stops a GitLab
        # project being probed with the GitHub CLI and read as "no open MRs".
        api=ForgePrApiClient(
            github=GhPrApiClient(),
            gitlab=GlabPrApiClient(token=gitlab_token),
        ),
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
    default) AND the active posture permits acting outward. Reviewing a colleague's MR
    IS posting on the owner's behalf, so ``afk`` skips the arm rather than queueing it:
    a queued review of a branch that moves is worth less than none, and self-review —
    the half that must not stall — is a different scanner.
    """
    from teatree.core.mode_resolution import egress_forbidden  # noqa: PLC0415 — deferred: ORM needs the app registry

    settings = _effective_settings_for_overlay(overlay_name)
    return settings.admit_colleague_prs_to_board and not egress_forbidden()


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
    api = GhCodexPrApi()
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


def _architectural_review_scanner_for(backend: OverlayBackends) -> ArchitecturalReviewScanner:
    """Build a per-overlay architectural-review scanner from teatree-core config.

    #1136 / #1152 re-architecture: the architectural-review cadence is a
    teatree-core platform behaviour that applies uniformly to every
    overlay's worktrees, NOT a per-overlay opt-in. The settings live on
    :class:`teatree.config.UserSettings` (DB-home in the ``ConfigSetting``
    store, with optional per-overlay overrides via the
    standard ``[overlays.<name>]`` shape — see
    ``OVERLAY_OVERRIDABLE_SETTINGS``). The scanner is instantiated once
    per registered overlay so each overlay's task queue gets its own
    cadence. Turning the review off is the ``arch_review`` Loop row (or a preset
    masking it), never a per-scanner flag.

    This does not skip overlays without a Python class — the scanner only needs
    ``backend.name`` to operate.
    """
    settings = _effective_settings_for_overlay(backend.name)
    return ArchitecturalReviewScanner(
        overlay_name=backend.name,
        skill=settings.architectural_review_skill,
        cadence_hours=settings.architectural_review_cadence_hours,
        after_merge_count=settings.architectural_review_after_merge_count,
    )


def _issue_intake_scanner_for(backend: OverlayBackends) -> IssueIntakeScanner | None:
    """Build the per-overlay unified intake scanner behind the triple gate (#3634).

    Returns a scanner ONLY when the intake loop is opted in for this overlay AND
    the in-flight budget has room. Two of the triple gate's three checks live
    here; the third — per-issue claim idempotency — lives in the scanner
    (:meth:`ImplementedIssueMarker.claim` returns ``None`` for an already-claimed
    issue).

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
    from teatree.core.intake import factory_admission  # noqa: PLC0415 — leaf import

    settings = _effective_settings_for_overlay(backend.name)
    code_host = backend.host
    if code_host is None:
        return None
    reconcile_holder_pr_rows_best_effort(backend.name)
    # #3275: self-heal the in-flight budget BEFORE reading it. A marker orphaned
    # while the pipeline was down never leaves ``dispatched``/``ticket_created``,
    # so it strands its slot and the budget gate reads false forever.
    ImplementedIssueMarker.objects.reconcile_stale(backend.name)
    limit = resolve_intake_concurrency(settings.issue_implementer_max_concurrent, overlay=backend.name)
    static_limit = settings.issue_implementer_max_concurrent
    budget = read_intake_budget(backend.name, limit, static_limit=static_limit)
    # #4389: a budget held entirely by claims going nowhere used to be detected, reported
    # and then waited out — every holder's own grace, with no issue admissible meanwhile.
    released = release_deadlocked_holder(budget)
    if released is not None:
        logger.warning("issue intake broke a deadlocked budget by releasing its longest-held slot: %s", released)
        budget = read_intake_budget(backend.name, limit, static_limit=static_limit)
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
        admit_label=settings.issue_implementer_label or factory_admission.DEFAULT_ADMIT_LABEL,
        overlay_name=backend.name,
        umbrella_labels=factory_admission.resolve_umbrella_labels(backend.name),
        trusted_authors=tuple(sorted(effective_trusted_issue_authors(settings))),
        identities=backend.identities,
        exclude_labels=backend.exclude_labels,
        repo_slugs=owned_repo_slugs(backend.overlay),
        can_claim=can_claim,
        max_concurrent=limit,
        pass_budget_seconds=settings.issue_intake_pass_budget_seconds,
    )


def _issue_disposition_scanner_for(backend: OverlayBackends) -> IssueDispositionScanner | None:
    """Build the issue-disposition scanner for the canonical core overlay (#2122).

    Returns a scanner ONLY for the canonical core overlay. Closing an issue is a judgement
    about someone's backlog, and this loop may make it only about repos teatree itself
    owns — the owner's rule is "only for t3-teatree owned repos", and it is
    POSTURE-INDEPENDENT: it holds in ``present`` exactly as it holds under an egress
    forbid, which is why it is a condition here rather than an egress opinion. The
    constraint is stated in the loop's shipped description too, so the loop file, the
    dash and the doctor all show it.

    Scoped at BOTH boundaries: only the canonical backend gets a scanner, and its
    assignee search carries the overlay's owned ``owner/repo`` slugs to the forge.
    An overlay with no owned-repo declaration scans nothing rather than falling back
    to the host's account-wide assigned-issue listing.

    This loop is also mechanical, so the owner's usual home for such a constraint — the
    loop's PROMPT — does not exist: its signals route to ``close_dead_issue``, which is
    physically unable to enqueue work. No agent, no prompt.

    Neither evidence bucket takes a repo from here: the listing spans every owned repo,
    so the duplicate search and the obsolescence oracle both follow each candidate to its
    own repo, and a repo with no local clone is unjudgeable and keeps its issue open.
    """
    if backend.name != CANONICAL_CORE_OVERLAY:
        return None
    code_host = backend.host
    if code_host is None:
        return None
    return IssueDispositionScanner(
        host=code_host,
        repo_slugs=owned_repo_slugs(backend.overlay),
        overlay_name=backend.name,
        identities=backend.identities,
        path_exists=_path_exists_in_own_clone,
    )


def _triage_assessor_scanner_for(backend: OverlayBackends) -> TriageAssessorScanner | None:
    """Build a per-overlay triage-assessor scanner behind its master gate.

    ``None`` when the overlay has no code host (nothing to list issues on) — whether the
    loop runs at all is the active preset's opinion. The scanner never writes to the
    host — it only queues an assessment task behind the ask-gate.
    """
    code_host = backend.host
    if code_host is None:
        return None
    return TriageAssessorScanner(
        host=code_host,
        overlay_name=backend.name,
        identities=backend.identities,
        repo_slugs=owned_repo_slugs(backend.overlay),
    )


def _mr_triage_scanner_for(backend: OverlayBackends, *, ci_enricher: CiEnricher) -> MrTriageScanner | None:
    """Build the MR-triage surveyor for an overlay with a code host.

    ``None`` when the overlay has no code host (no MRs to read). The nag-patience
    inputs are resolved from the same overlay hook the review nag uses, so the two can
    never disagree about how long a repo waits.

    *ci_enricher* is required, not optional: GitLab's MR list payload carries no
    pipeline, so a surveyor without one reads UNKNOWN for every merge request on that
    forge. The caller passes the enricher it already holds so the per-tick read budget
    stays one shared bound rather than one per scanner.
    """
    code_host = backend.host
    if code_host is None:
        return None
    overlay = backend.overlay
    return MrTriageScanner(
        host=code_host,
        overlay_name=backend.name,
        identities=backend.identities,
        allowed_url_prefixes=_allowed_url_prefixes_for_host(backend, code_host),
        repo_owner=overlay.review.repo_owner_for_slug if overlay is not None else default_repo_owner,
        ci_enricher=ci_enricher,
    )


def _mr_conflict_scanner_for(backend: OverlayBackends, code_host: CodeHostBackend) -> MrConflictScanner:
    """Build the per-host merge-conflict sweep.

    Per HOST rather than per overlay because the conflict probe is a forge call:
    it must go to the host that lists the merge request, and an overlay with both
    a GitHub and a GitLab credential lists on both.
    """
    return MrConflictScanner(
        host=code_host,
        identities=backend.identities,
        allowed_url_prefixes=_allowed_url_prefixes_for_host(backend, code_host),
        overlay_name=backend.name,
    )


def _path_exists_in_own_clone(repo: str, path: str) -> bool | None:
    """Whether *path* exists in *repo*'s own clone; ``None`` when no clone of *repo* resolves.

    Another repo's clone says nothing about this one, and an absent clone cannot prove a
    path gone, so neither may count as evidence for closing an issue.
    """
    clone = find_clone_path(clone_root(), repo)
    return None if clone is None else (clone / path).exists()


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
