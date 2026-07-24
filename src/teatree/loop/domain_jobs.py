"""Per-overlay domain job slices + the domain dispatch table.

Each ``Domain`` member's job slice, the dispatch dicts, ``jobs_for_domain`` (the
typed seam the mini-loops consume), and the per-tick error/run helpers. Depends
DOWN on ``scanner_factories`` (the scanner constructors) and ``job_identity``.
Carved out of the loop tick fan-out to stay under the module-health LOC cap.
"""

import datetime as _dt
import logging
from collections.abc import Callable

from teatree.core.backend_factory import OverlayBackends, messaging_from_overlay
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import NotifyKind, resolve_user_id
from teatree.loop.domain_optional_scanner_jobs import (
    _arch_review_jobs_for_overlay,
    _audit_jobs_for_overlay,
    _housekeeping_jobs_for_overlay,
    _issue_disposition_jobs_for_overlay,
    _issue_implementer_jobs_for_overlay,
    _triage_assessor_jobs_for_overlay,
)
from teatree.loop.job_identity import PER_OVERLAY_DOMAINS, Domain, _ScannerJob
from teatree.loop.scanner_factories import (
    _admit_colleague_prs_to_board,
    _competing_url_prefixes,
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
    PendingTasksScanner,
    PrApprovalScanner,
    RedCardScanner,
    ReviewDoneAckScanner,
    ReviewedPrHeadScanner,
    ReviewerPrsScanner,
    ReviewNagScanner,
    ReviewRequestMergeReactScanner,
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
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host, _identity_alias_groups_for_overlay
from teatree.messaging import notify_with_fallback

logger = logging.getLogger(__name__)


def default_drift_notifier(alert_text: str, idempotency_key: str) -> None:
    """Production drift-notifier: post via the overlay bot, idempotent on key.

    Uses the verified-delivery wrapper (#1181) so a silent primary
    ``notify_user`` failure (the #1173 class) auto-falls back to a direct,
    round-trip-verified send instead of dropping the drift alert. Lives here
    (the orchestration construction site) rather than inside the
    ``outbound_audit`` scanner so the scanner stays in the ``domain`` layer
    with no ``messaging``/``notify`` (``integration``) up-edge — it is
    injected into :class:`OutboundAuditScanner` at construction.
    """
    notify_with_fallback(
        alert_text, kind=NotifyKind.INFO, idempotency_key=idempotency_key, audience=NotifyAudience.OWNER_ESCALATION
    )


def _global_dispatch_jobs() -> list[_ScannerJob]:
    """The always-on global set ``build_default_jobs`` fans out once per tick.

    The two owner-DM delivery scanners (``UndeliveredNotifyScanner`` and
    ``DeferredQuestionPosterScanner``) are handed an explicit messaging backend +
    user id resolved from the active overlay — the same source
    ``_pr_sweep_scanner_for`` uses — so a global tick with no ``T3_OVERLAY_NAME``
    can still DELIVER the allowed owner-audience DMs instead of no-opping on an
    unresolved backend (F2). A ``None`` backend (no overlay configured) leaves the
    scanners on ``notify_user``'s own resolution, unchanged.
    """
    backend = messaging_from_overlay()
    user_id = resolve_user_id()
    return [
        _ScannerJob(scanner=PendingTasksScanner(), overlay=""),
        _ScannerJob(scanner=IncomingEventsScanner(), overlay=""),
        _ScannerJob(scanner=OutboundAuditScanner(notifier=default_drift_notifier), overlay=""),
        _ScannerJob(scanner=UndeliveredNotifyScanner(backend=backend, user_id=user_id), overlay=""),
        _ScannerJob(scanner=DeferredQuestionPosterScanner(backend=backend, user_id=user_id), overlay=""),
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
    """Own-author PR scanner + the auto-merge PR sweep + (opt-in) GitLab-approvals poll, per host.

    #3244: the ``pr_sweep`` auto-merge engine lives HERE, in the ship domain, not
    the review domain. The review loop is ``colleague_facing`` and is SKIPPED under
    ``autonomous_away`` (loop_table gates it on availability), which starved the
    merge path exactly when the operator was away. Ship is enabled and ticks every
    5m, and its seed already claims the keystone merge, so the sweep belongs with it.
    """
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
    sweep_scanner = _pr_sweep_scanner_for(backend, slack_user_id=_user_slack_id_for_overlay(tag))
    if sweep_scanner is not None:
        jobs.append(_ScannerJob(scanner=sweep_scanner, overlay=tag))
    return jobs


def _review_jobs_for_overlay(
    backend: OverlayBackends,
    *,
    all_backends: tuple[OverlayBackends, ...],
) -> list[_ScannerJob]:
    """The single review intake (#3569): self-authored + colleague PRs → one board.

    Both feed the SAME ``reviewing`` → ``t3:reviewer`` (Claude) queue; the review
    execution is blind to author. The author distinction lives HERE, upstream.

    Self-authored open PRs are ALWAYS admitted — the ``ClaudeSelfPrReviewScanner``
    sweeps the owner's own open PRs and enqueues one Claude ``reviewing`` task per
    un-reviewed head SHA (per-SHA dedup = "since last review"); codex is no longer
    the self-review mechanism. COLLEAGUE / requested-reviewer PRs are admitted only
    when ``admit_colleague_prs_to_board`` is ON (the sole config knob) — the
    ``ReviewerPrsScanner`` is built only then, so ``false`` keeps colleague PRs off
    the board while self-review still runs.

    #3244: the ``pr_sweep`` auto-merge engine lives in the ship domain, not here.
    """
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
    """Return the scanner-job slice *domain* owns (#1482).

    The public, typed seam the mini-loops consume in place of reaching
    into the loop fan-out's privates. The per-overlay members
    (:data:`PER_OVERLAY_DOMAINS`) partition :func:`_jobs_for_overlay_backend`
    — disjoint and exhaustive — and require *backend*. ``Domain.DISPATCH``
    is the global dispatch set and ignores *backend* (it carries no
    per-overlay state), so callers with no overlay context pass none.

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
    so :func:`teatree.core.notify.notify_user`'s ``BotPing`` ledger dedups
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
        notify_with_fallback(text, kind=NotifyKind.INFO, idempotency_key=key, audience=NotifyAudience.OWNER_ESCALATION)
    except Exception:
        logger.exception("Scanner-error notify_with_fallback failed for %s", label)


def _inbound_messaging_jobs(messaging: MessagingBackend, tag: str) -> list[_ScannerJob]:
    """The inbound-messaging scanner jobs (mentions / DM / ask-reply / review-intent / red-card), sans nag.

    The single ordered inbound scanner set both the per-overlay
    :func:`_messaging_jobs_for_backend` and the single-overlay
    :func:`single_overlay_messaging_jobs` build from, so the two paths cannot
    re-diverge on the inbound set (#23) — a new inbound scanner is added HERE
    once and every messaging fan-out picks it up.

    ``SlackMentionsScanner`` owns the JSONL drain and fans reaction events into
    the backend's reactions queue; ``SlackReviewIntentScanner`` must run after it
    so the queue is populated for the same tick. ``SlackReviewIntentScanner`` is
    also the SINGLE owner of the ``slack-reactions.jsonl`` atomic-rename drain, so
    the 👀-back self-ack (owner reacts to teatree's OWN message → 👀 back) rides
    INSIDE it — consuming the same drained snapshot rather than racing a second
    drain (#1047).
    """
    return [
        _ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=tag),
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
    """Single-overlay (``overlay=""``) inbound-messaging scanner jobs — the #23 SSOT.

    Both single-overlay callers import THIS builder — the inbox mini-loop's
    single-overlay branch and ``build_default_jobs``' single-overlay messaging
    branch — so they can never re-diverge on the inbound scanner set (the #23
    drift, where both had dropped ``AskUserQuestionReplyScanner``). It is the
    ``overlay=""`` projection of the same inbound scanners
    :func:`_messaging_jobs_for_backend` fans out per overlay minus
    ``ReviewNagScanner``, pinned identical by the coverage parity lane. A later
    single-overlay inbound scanner registers by extending
    :func:`_inbound_messaging_jobs`, never by re-forking this builder.
    """
    return _inbound_messaging_jobs(messaging, "")


def _messaging_jobs_for_backend(
    backend: OverlayBackends,
    tag: str,
    *,
    include_review_nag: bool = True,
) -> list[_ScannerJob]:
    """Per-overlay Slack scanners that need a resolved messaging backend.

    Caller must check ``backend.messaging is not None`` before invoking; a
    defensive early-return keeps the type narrow without a bare ``assert``.

    ``include_review_nag`` lets a high-cadence caller (the inbox mini-loop) drop
    ``ReviewNagScanner`` so the nag is emitted by exactly one owner — the followup
    mini-loop, whose 10-minute cadence matches the legacy single emission. The
    legacy monolithic fan-out keeps the default.
    """
    messaging = backend.messaging
    if messaging is None:
        return []
    jobs = _inbound_messaging_jobs(messaging, tag)
    if include_review_nag:
        nag = ReviewNagScanner(
            messaging=messaging,
            host=backend.host,
            identities=backend.identities,
        )
        jobs.append(_ScannerJob(scanner=nag, overlay=tag))
    return jobs
