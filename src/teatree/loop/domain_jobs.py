"""Per-overlay domain job slices + the domain dispatch table.

Each ``Domain`` member's job slice, the dispatch dicts, ``jobs_for_domain`` (the
typed seam the mini-loops consume), and the per-tick error/run helpers. Depends
DOWN on ``scanner_factories`` (the scanner constructors) and ``job_identity``.
Carved out of the loop tick fan-out to stay under the module-health LOC cap.
"""

import logging
from collections.abc import Callable

from teatree.core.backend_factory import OverlayBackends, messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind, resolve_user_id
from teatree.core.review.mr_triage import RepoOwner
from teatree.loop.domain_optional_scanner_jobs import (
    _arch_review_jobs_for_overlay,
    _audit_jobs_for_overlay,
    _housekeeping_jobs_for_overlay,
    _issue_disposition_jobs_for_overlay,
    _issue_implementer_jobs_for_overlay,
    _triage_assessor_jobs_for_overlay,
)
from teatree.loop.job_identity import PER_OVERLAY_DOMAINS, Domain, _ScannerJob
from teatree.loop.scanner_error_notice import notify_scanner_error
from teatree.loop.scanner_factories import (
    _admit_colleague_prs_to_board,
    _competing_url_prefixes,
    _mr_conflict_scanner_for,
    _mr_triage_scanner_for,
    _pr_sweep_scanner_for,
    _self_pr_review_scanner_for,
    _slack_broadcasts_scanner_for,
    _task_sweep_scanner_for,
)
from teatree.loop.scanner_factory_config import (
    _gitlab_approvals_enabled,
    _user_identity_aliases_for_overlay,
    _user_slack_id_for_overlay,
    stranger_pr_admission,
)
from teatree.loop.scanners import (
    ActiveTicketsScanner,
    AskUserQuestionReplyScanner,
    DeferredQuestionPosterScanner,
    GitLabApprovalsScanner,
    IncomingEventsScanner,
    MyPrsScanner,
    OutboundAuditScanner,
    PendingPrDrainScanner,
    PendingTasksScanner,
    PrApprovalScanner,
    QuestionBacklogNagScanner,
    RedCardScanner,
    ReviewDoneAckScanner,
    ReviewedPrHeadScanner,
    ReviewerPrsScanner,
    ReviewNagScanner,
    ReviewRequestMergeReactScanner,
    ReviewRequestResumeScanner,
    ScanSignal,
    SlackDmInboundScanner,
    SlackMentionsScanner,
    SlackReviewIntentScanner,
    StaleTicketsScanner,
    TicketCompletionScanner,
    TicketDispositionScanner,
    UndeliveredNotifyScanner,
    WaitingDigestScanner,
    WorkStateScanner,
)
from teatree.loop.scanners.base import ScannerError
from teatree.loop.scanners.my_prs_ci import BoundedCiEnricher
from teatree.loop.scanners.review_nag import default_repo_owner
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host, _identity_alias_groups_for_overlay
from teatree.messaging import notify_with_fallback

logger = logging.getLogger(__name__)


def default_drift_notifier(alert_text: str, idempotency_key: str) -> None:
    """Production drift-notifier: post via the overlay bot, idempotent on key."""
    notify_with_fallback(
        alert_text, kind=NotifyKind.INFO, idempotency_key=idempotency_key, audience=NotifyAudience.OWNER_ESCALATION
    )


def _global_dispatch_jobs() -> list[_ScannerJob]:
    """The always-on global set ``build_default_jobs`` fans out once per tick."""
    backend = messaging_from_overlay()
    user_id = resolve_user_id()
    return [
        _ScannerJob(scanner=PendingTasksScanner(), overlay=""),
        _ScannerJob(scanner=IncomingEventsScanner(), overlay=""),
        _ScannerJob(scanner=OutboundAuditScanner(notifier=default_drift_notifier), overlay=""),
        _ScannerJob(scanner=PendingPrDrainScanner(), overlay=""),
        _ScannerJob(scanner=UndeliveredNotifyScanner(backend=backend, user_id=user_id), overlay=""),
        _ScannerJob(scanner=DeferredQuestionPosterScanner(backend=backend, user_id=user_id), overlay=""),
        _ScannerJob(scanner=QuestionBacklogNagScanner(backend=backend, user_id=user_id), overlay=""),
        _ScannerJob(scanner=WaitingDigestScanner(), overlay=""),
        # SELFCATCH-1: global (walks every ticket across overlays via
        # ``reconcile_work_state_all``), so it runs once per tick here rather
        # than redundantly once per overlay in the housekeeping domain.
        _ScannerJob(scanner=WorkStateScanner(), overlay=""),
    ]


def _tickets_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """Local Ticket-DB scanners + per-host disposition/completion + TODO sweep."""
    tag = backend.name
    jobs: list[_ScannerJob] = []
    if backend.external_db is not None:
        from teatree.loop.scanners.external_tickets import ExternalTicketsScanner  # noqa: PLC0415 — tick-time import

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
    task_sweep_scanner = _task_sweep_scanner_for(backend)
    if task_sweep_scanner is not None:
        jobs.append(_ScannerJob(scanner=task_sweep_scanner, overlay=tag))
    return jobs


def _tickets_per_host_jobs(backend: OverlayBackends, tag: str) -> list[_ScannerJob]:
    """Per-host disposition scanner + the once-per-overlay completion scanner."""
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
    """Own-author PR scanner + the auto-merge PR sweep + (opt-in) GitLab-approvals poll, per host."""
    tag = backend.name
    gitlab_approvals_enabled = _gitlab_approvals_enabled()
    jobs: list[_ScannerJob] = []
    # One enricher for the whole overlay: its per-tick budget is shared across the
    # hosts below rather than multiplied by them, and this builder runs once a tick.
    ci_enricher = BoundedCiEnricher()
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
                    ci_enricher=ci_enricher,
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
        # Every open merge request owes a resolved conflict whatever its review
        # policy says, so the sweep rides the ship domain alongside the merge
        # engine rather than the colleague-facing review loop the away posture
        # skips. Default-OFF: the builder returns None until an overlay opts in.
        conflict_scanner = _mr_conflict_scanner_for(backend, code_host)
        if conflict_scanner is not None:
            jobs.append(_ScannerJob(scanner=conflict_scanner, overlay=tag))
    sweep_scanner = _pr_sweep_scanner_for(backend, slack_user_id=_user_slack_id_for_overlay(tag))
    if sweep_scanner is not None:
        jobs.append(_ScannerJob(scanner=sweep_scanner, overlay=tag))
    triage_scanner = _mr_triage_scanner_for(backend)
    if triage_scanner is not None:
        jobs.append(_ScannerJob(scanner=triage_scanner, overlay=tag))
    return jobs


def _review_jobs_for_overlay(
    backend: OverlayBackends,
    *,
    all_backends: tuple[OverlayBackends, ...],
) -> list[_ScannerJob]:
    """The single review intake (#3569): self-authored + colleague PRs → one board."""
    tag = backend.name
    jobs: list[_ScannerJob] = []
    self_pr_scanner = _self_pr_review_scanner_for(backend)
    if self_pr_scanner is not None:
        jobs.append(_ScannerJob(scanner=self_pr_scanner, overlay=tag))
    if _admit_colleague_prs_to_board(tag):
        reviewer_trusted, reviewer_admit_label = stranger_pr_admission(tag)
        for code_host in backend.hosts:
            url_prefixes = _allowed_url_prefixes_for_host(backend, code_host)
            competing_prefixes = _competing_url_prefixes(
                this_backend=backend,
                code_host=code_host,
                all_backends=all_backends,
            )
            # A colleague MR discovered from a Slack broadcast never gets a forge
            # reviewer assignment, so ``ReviewerPrsScanner`` (a
            # ``list_review_requested_prs`` filter) is structurally blind to it
            # after the first pass. ``ReviewedPrHeadScanner`` watches the LOCAL
            # reviewer tickets instead, so a discharged review re-opens on a new
            # head whatever route discovered it.
            jobs.extend(
                (
                    _ScannerJob(
                        scanner=ReviewerPrsScanner(
                            host=code_host,
                            identities=backend.identities,
                            overlay_name=tag,
                            allowed_url_prefixes=url_prefixes,
                            competing_url_prefixes=competing_prefixes,
                            trusted_authors=reviewer_trusted,
                            admit_label=reviewer_admit_label,
                        ),
                        overlay=tag,
                    ),
                    _ScannerJob(
                        scanner=ReviewedPrHeadScanner(
                            host=code_host,
                            overlay_name=tag,
                            allowed_url_prefixes=url_prefixes,
                            competing_url_prefixes=competing_prefixes,
                        ),
                        overlay=tag,
                    ),
                )
            )
    broadcasts_scanner = _slack_broadcasts_scanner_for(backend)
    if broadcasts_scanner is not None:
        jobs.append(_ScannerJob(scanner=broadcasts_scanner, overlay=tag))
    if backend.messaging is not None:
        # The colleague-visible review-DONE ack. Binding it to the reviewer
        # ticket's DELIVERED state (not to an optional ``review record`` CLI
        # call) is what makes a completed review visible to colleagues at all.
        jobs.append(
            _ScannerJob(
                scanner=ReviewDoneAckScanner(messaging=backend.messaging, overlay_name=tag),
                overlay=tag,
            ),
        )
    return jobs


def _repo_owner_resolver(backend: OverlayBackends) -> Callable[[str], RepoOwner]:
    """The overlay's repo-ownership answer, or core's when the overlay has no class."""
    overlay = backend.overlay
    if overlay is None:
        return default_repo_owner
    return overlay.review.repo_owner_for_slug


def _followup_jobs_for_overlay(backend: OverlayBackends) -> list[_ScannerJob]:
    """The single review-nag (overlay-scoped). Intake is the unified ``issue_intake`` job."""
    tag = backend.name
    jobs: list[_ScannerJob] = []
    if backend.messaging is not None:
        jobs.extend(
            (
                _ScannerJob(
                    scanner=ReviewNagScanner(
                        messaging=backend.messaging,
                        host=backend.host,
                        identities=backend.identities,
                        repo_owner=_repo_owner_resolver(backend),
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
                _ScannerJob(
                    scanner=ReviewRequestResumeScanner(
                        messaging=backend.messaging,
                        host=backend.host,
                        overlay=tag,
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


def _identity_groups_for_overlay(backend: OverlayBackends) -> tuple[tuple[str, ...], ...]:
    """Resolve disposition identity-alias groups with the multi-identity self-group fallback (#1113)."""
    groups = _identity_alias_groups_for_overlay(backend.name, backend)
    if not groups and len(backend.identities) > 1:
        return (tuple(backend.identities),)
    return groups


type _OverlayDomainBuilder = Callable[[OverlayBackends], list[_ScannerJob]]


type _UrlAwareDomainBuilder = Callable[..., list[_ScannerJob]]


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
    Domain.ISSUE_DISPOSITION: _issue_disposition_jobs_for_overlay,
    Domain.TRIAGE_ASSESSOR: _triage_assessor_jobs_for_overlay,
}


def jobs_for_domain(
    domain: Domain,
    backend: OverlayBackends | None = None,
    *,
    all_backends: tuple[OverlayBackends, ...] = (),
) -> list[_ScannerJob]:
    """Return the scanner-job slice *domain* owns (#1482)."""
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
    """Build every scanner job that fans out for one overlay backend."""
    jobs: list[_ScannerJob] = []
    for domain in PER_OVERLAY_DOMAINS:
        jobs.extend(jobs_for_domain(domain, backend, all_backends=all_backends))
    return jobs


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
        notify_scanner_error(label=label, exc=exc, overlay=job.overlay)
        return label, [], f"ScannerError[{exc.error_class.value}]: {exc.detail or exc}"
    except Exception as exc:
        logger.exception("Scanner %s raised", label)
        return label, [], f"{type(exc).__name__}: {exc}"
    return label, signals, ""


def _inbound_messaging_jobs(messaging: MessagingBackend, tag: str) -> list[_ScannerJob]:
    """The inbound-messaging scanner jobs (mentions / DM / ask-reply / review-intent / red-card), sans nag."""
    return [
        _ScannerJob(scanner=SlackMentionsScanner(backend=messaging, overlay=tag), overlay=tag),
        _ScannerJob(scanner=SlackDmInboundScanner(backend=messaging, overlay=tag), overlay=tag),
        # #1174 applies each Slack reply to its live DeferredQuestion — the
        # scanner the two single-overlay builders had silently dropped (#23).
        _ScannerJob(scanner=AskUserQuestionReplyScanner(backend=messaging, overlay=tag), overlay=tag),
        # Owns the reactions-JSONL drain; the 👀-back self-ack rides inside it.
        _ScannerJob(scanner=SlackReviewIntentScanner(backend=messaging, overlay=tag), overlay=tag),
        # #1130 RED CARD detection — user's structural "fix it upstream"
        # signal. Runs alongside the review-intent scanner because both
        # drain reactions; this one only cares about ``:red_circle:`` /
        # ``:no_entry_sign:`` plus the literal phrase in DMs.
        _ScannerJob(scanner=RedCardScanner(backend=messaging, overlay=tag), overlay=tag),
        # #8: forge-approval poll that revives the M7 merge_authorization
        # waiting lane — drives REVIEW_REQUESTED PRs to APPROVED so the
        # waiting-digest DM + the (on-behalf-gated) #961 approval reaction fire.
        # Resolves its own code host from the overlay; no messaging dependency.
        _ScannerJob(scanner=PrApprovalScanner(overlay=tag), overlay=tag),
    ]


def single_overlay_messaging_jobs(messaging: MessagingBackend) -> list[_ScannerJob]:
    """Single-overlay (``overlay=""``) inbound-messaging scanner jobs — the #23 SSOT."""
    return _inbound_messaging_jobs(messaging, "")


def _messaging_jobs_for_backend(
    backend: OverlayBackends,
    tag: str,
    *,
    include_review_nag: bool = True,
) -> list[_ScannerJob]:
    """Per-overlay Slack scanners that need a resolved messaging backend."""
    messaging = backend.messaging
    if messaging is None:
        return []
    jobs = _inbound_messaging_jobs(messaging, tag)
    if include_review_nag:
        nag = ReviewNagScanner(
            messaging=messaging,
            host=backend.host,
            identities=backend.identities,
            repo_owner=_repo_owner_resolver(backend),
        )
        jobs.append(_ScannerJob(scanner=nag, overlay=tag))
    return jobs
