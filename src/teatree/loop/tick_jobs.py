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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.config import discover_active_overlay, discover_overlays, load_config

if TYPE_CHECKING:
    from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.loop.scanners import (
    ActiveTicketsScanner,
    ArchitecturalReviewScanner,
    AssignedIssuesScanner,
    BackendChannelHistoryFetcher,
    CallCommandMergeKeystone,
    GhPrApiClient,
    GitLabApprovalsScanner,
    GlabGhMrStateClassifier,
    IncomingEventsScanner,
    MyPrsScanner,
    NotionViewScanner,
    NullMergeNotifier,
    OutboundAuditScanner,
    PendingTasksScanner,
    PrSweepScanner,
    RedCardScanner,
    ReviewerPrsScanner,
    ReviewNagScanner,
    Scanner,
    ScanningNewsScanner,
    SlackBroadcastsScanner,
    SlackDmInboundScanner,
    SlackMentionsScanner,
    SlackMergeNotifier,
    SlackReviewIntentScanner,
    StaleTicketsScanner,
    TicketCompletionScanner,
    TicketDispositionScanner,
)
from teatree.loop.scanners.base import ScannerError, ScanSignal
from teatree.loop.scanners.notion_view import NotionLike
from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host, _identity_alias_groups_for_overlay
from teatree.notify import NotifyKind, notify_user

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ScannerJob:
    """Internal record pairing a scanner with its overlay tag."""

    scanner: Scanner
    overlay: str


def _jobs_for_backend_hosts(backend: OverlayBackends, tag: str) -> list[_ScannerJob]:
    """Build one scanner-job fan-out per host on *backend* (#976).

    Pre-fix the caller assumed one ``backend.host``; with multi-host the
    same fan-out must run for each platform that resolved a credential.
    ``TicketCompletionScanner`` is overlay-scoped (reads local Ticket
    rows), so it's emitted exactly once even when two hosts are present.
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
        jobs.extend(
            [
                _ScannerJob(
                    scanner=MyPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        allowed_url_prefixes=url_prefixes,
                    ),
                    overlay=tag,
                ),
                _ScannerJob(
                    scanner=ReviewerPrsScanner(
                        host=code_host,
                        identities=backend.identities,
                        overlay_name=tag,
                        allowed_url_prefixes=url_prefixes,
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
    MR-link broadcasts so a reviewer-role tag in a Slack-Connect review
    crew triggers the same downstream dispatch as a direct ``:eyes:``
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
    return SlackBroadcastsScanner(
        backend=backend.messaging,
        channels=channel_ids,
        fetch_channel_history=BackendChannelHistoryFetcher(backend=backend.messaging),
        classify_mrs=GlabGhMrStateClassifier(glab_token=glab_token, github_token=github_token),
        overlay=backend.name,
    )


def _pr_sweep_scanner_for(backend: OverlayBackends, *, slack_user_id: str) -> PrSweepScanner | None:
    """Build a per-overlay PR-sweep scanner from the overlay's followup repos (#1257).

    The scanner merges green-and-cleared PRs on the overlay's GitHub repos
    every tick. Repo list is sourced from
    ``overlay.metadata.get_followup_repos()`` — the same accessor docgen
    and skill-sync already use for "cross-repo work scoped to this
    overlay". Returns ``None`` when the overlay has no Python class
    (TOML-only) or no GitHub repos configured; the loop must skip
    cleanly in those cases.
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
    return PrSweepScanner(
        repos=repos,
        api=GhPrApiClient(token=github_token),
        keystone=CallCommandMergeKeystone(),
        notifier=notifier,
        overlay=backend.name,
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


#: Canonical fallback overlay anchor (#1267 / migration 0027). The
#: bundled teatree overlay registers via the ``teatree.overlays`` entry
#: point under this name; ``discover_active_overlay()`` resolves it in
#: ordinary installations. The literal here is a defensive default for
#: machines with no overlay registered — it is not consulted by the
#: scanner itself, which only ever sees the resolved string.
_CANONICAL_CORE_OVERLAY = "t3-teatree"


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
    )


def _effective_settings_for_overlay(overlay_name: str) -> "UserSettings":
    """Resolve :class:`UserSettings` honouring this overlay's ``[overlays.<name>]`` overrides.

    Mirrors ``get_effective_settings()`` but resolves the active overlay
    explicitly by name (the scanner-builder loops over every registered
    overlay, not just the one in ``T3_OVERLAY_NAME``). Falls back to the
    global ``[teatree]`` values when no per-overlay override is set.
    """
    from dataclasses import replace  # noqa: PLC0415

    base = load_config().user
    for entry in discover_overlays():
        if entry.name == overlay_name and entry.overrides:
            return replace(base, **entry.overrides)
    return base


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
        notify_user(text, kind=NotifyKind.INFO, idempotency_key=key)
    except Exception:
        logger.exception("Scanner-error notify_user failed for %s", label)


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


def _jobs_for_overlay_backend(backend: OverlayBackends) -> list[_ScannerJob]:
    """Build every scanner job that fans out for one overlay backend.

    Split out of :func:`build_default_jobs` to keep the orchestrator
    under the cyclomatic-complexity gate; the per-overlay shape is:
    active-or-external tickets, stale tickets, per-host PR/issue
    scanners, architectural review, PR sweep, slack broadcasts, and
    the messaging-dependent slack scanners.
    """
    jobs: list[_ScannerJob] = []
    tag = backend.name
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
    # Multi-host: an overlay with both GitHub and GitLab PATs scans both
    # forges, so PRs on one platform don't drown out PRs on the other
    # (#976). The single-host path is preserved by iterating
    # ``backend.hosts`` (which is empty when no token resolved).
    jobs.extend(_jobs_for_backend_hosts(backend, tag))
    # #1136 / #1152 Periodic architectural-review scanner. CORE
    # always-on for every registered overlay; the cadence lives in
    # teatree-core config since architectural review applies uniformly
    # to all overlays' worktrees, not as a per-overlay opt-in.
    arch_scanner = _architectural_review_scanner_for(backend)
    if arch_scanner is not None:
        jobs.append(_ScannerJob(scanner=arch_scanner, overlay=tag))
    # #1257 PR-sweep scanner — auto-merge-green-PRs sibling wired
    # per-overlay (not per-host). The overlay's followup-repos list
    # (full ``owner/repo`` slugs) is the sweep target.
    sweep_scanner = _pr_sweep_scanner_for(backend, slack_user_id=_user_slack_id_for_overlay(tag))
    if sweep_scanner is not None:
        jobs.append(_ScannerJob(scanner=sweep_scanner, overlay=tag))
    # #1255 Slack broadcast scanner — polls the overlay's review
    # channel for MR-link broadcasts and dispatches reviewer-role work
    # to the existing review-intent pipeline.
    broadcasts_scanner = _slack_broadcasts_scanner_for(backend)
    if broadcasts_scanner is not None:
        jobs.append(_ScannerJob(scanner=broadcasts_scanner, overlay=tag))
    # #1295 cap E: failed-E2E Slack-post scanner; the overlay supplies
    # watchers via ``OverlayConfig.get_failed_e2e_watchers``.
    failed_e2e_scanner = _failed_e2e_scanner_for(backend)
    if failed_e2e_scanner is not None:
        jobs.append(_ScannerJob(scanner=failed_e2e_scanner, overlay=tag))
    if backend.messaging is not None:
        jobs.extend(_messaging_jobs_for_backend(backend, tag))
    return jobs


def _failed_e2e_scanner_for(backend: OverlayBackends) -> Scanner | None:
    """Build a per-overlay failed-E2E scanner from overlay watchers (#1295 cap E)."""
    from teatree.loop.scanners.failed_e2e_posts import failed_e2e_scanner_for  # noqa: PLC0415

    return failed_e2e_scanner_for(backend)


def _messaging_jobs_for_backend(backend: OverlayBackends, tag: str) -> list[_ScannerJob]:
    """Per-overlay Slack scanners that need a resolved messaging backend.

    ``SlackMentionsScanner`` owns the JSONL drain and fans reaction
    events into the backend's reactions queue; ``SlackReviewIntentScanner``
    must run after it so the queue is populated for the same tick.
    Caller must check ``backend.messaging is not None`` before invoking;
    a defensive early-return keeps the type narrow without a bare
    ``assert``.
    """
    messaging = backend.messaging
    if messaging is None:
        return []
    return [
        _ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=tag),
        _ScannerJob(scanner=SlackDmInboundScanner(backend=messaging, overlay=tag), overlay=tag),
        _ScannerJob(scanner=SlackReviewIntentScanner(backend=messaging, overlay=tag), overlay=tag),
        # #1130 RED CARD detection — user's structural "fix it upstream"
        # signal. Runs alongside the review-intent scanner because both
        # drain reactions; this one only cares about ``:red_circle:`` /
        # ``:no_entry_sign:`` plus the literal phrase in DMs.
        _ScannerJob(scanner=RedCardScanner(backend=messaging, overlay=tag), overlay=tag),
        _ScannerJob(
            scanner=ReviewNagScanner(messaging=messaging, user_slack_id=_user_slack_id_for_overlay(tag)),
            overlay=tag,
        ),
    ]


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
    jobs: list[_ScannerJob] = [
        _ScannerJob(scanner=PendingTasksScanner(), overlay=""),
        _ScannerJob(scanner=IncomingEventsScanner(), overlay=""),
        _ScannerJob(scanner=OutboundAuditScanner(), overlay=""),
    ]
    # #1191 Periodic scanning-news scanner — teatree-CORE global (not
    # per-overlay). Daily cadence is teatree-platform config; the queued
    # task is anchored on the `teatree` overlay placeholder ticket so
    # the dispatcher routes through the standard pending-task pipeline.
    news_scanner = _scanning_news_scanner()
    if news_scanner is not None:
        jobs.append(_ScannerJob(scanner=news_scanner, overlay=""))

    if backends:
        for backend in backends:
            jobs.extend(_jobs_for_overlay_backend(backend))
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
